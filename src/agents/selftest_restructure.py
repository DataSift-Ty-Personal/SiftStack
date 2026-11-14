"""Restructure selftest: the two tracks (Priority and Agency) built as one change list, the proofs
that stand in front of it, and apply and undo against a fake account. Zero network: the fake answers
every route the tool uses, refuses any other, and like the real search ignores a filter key it does
not know. The folder and preset titles are the account's structure, which the tool reads by name;
every record, owner, id and value here is invented.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import uuid as _uuid
from datetime import date
from pathlib import Path

DAY = date(2026, 11, 13)
ACCOUNT = "acct-1"


def _id(kind: str, title: str) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_DNS, "restructure-selftest:%s:%s" % (kind, title)))


TAGS = ["FTM", "Priority 1", "Priority 2", "recently sold", "Mail Only", "Not Single Family", "Do Not Market"]
LISTS = ["Low Equity", "Negative Equity", "DNC", "Obituary"]
HOT_X = ["recently sold", "Mail Only", "Not Single Family", "Do Not Market"]
STATUS_X = ["sold", "not_interested", "new_lead", "Warm Lead"]
BASE_LISTS = ("Low Equity", "Negative Equity", "DNC")
PUBLISHED = [("dead_lead", ["Dead Lead"]), ("new_lead", ["new_lead"]), ("not_interested", ["not_interested"]),
             ("qualified_lead", ["Cold Lead", "Warm Lead", "Hot Lead"]), ("unqualified_lead", ["No Contact New Lead"])]
RELATIVE = re.compile(r"^(\d+-)?(day|days|week|weeks|month|months|quarter|quarters|year|years)$|^(today|yesterday)$")


def _not(tags, lists=BASE_LISTS):
    return {"any_tags": [_id("tag", t) for t in tags], "any_lists": [_id("list", x) for x in lists],
            "any_neighborhood": ["Elm Hollow"], "any_property_status": list(STATUS_X)}


def _call(anchor, attempt, excl, lists=BASE_LISTS):
    m: dict = {"phone": 1}
    if anchor:
        m["any_tags"] = [_id("tag", anchor)]
    m.update({"must_not": _not(excl, lists), "skiptraced": 1})
    if attempt == 0:
        m["ownerPropertiesOwned"] = {"show_properties": "one_per_owner"}
    m["predictivecall_attempts"] = [attempt, attempt]
    m["any_phone_status"] = ["CORRECT", "NO_ANSWER", "UNKNOWN"]
    return m


def _needs(anchor, excl, traced=0):
    m: dict = {"phone": 0}
    if anchor:
        m["any_tags"] = [_id("tag", anchor)]
    m.update({"must_not": _not(excl), "skiptraced": traced, "predictivecall_attempts": [0, 0]})
    return m


def _mail(anchor, n, excl):
    m: dict = {}
    if anchor:
        m["any_tags"] = [_id("tag", anchor)]
    m.update({"must_not": _not(excl), "owner_vacant": 0, "directmail_attempts": [n, n]})
    if n:
        m["last_direct_mailed"] = ["36-months", "month"]
    return m


def account(strong_lists=BASE_LISTS, extra=None):
    """folder title -> [(preset title, saved filter)] in the order the account shows them."""
    strong_x = HOT_X + ["Priority 1"]
    bulk_x = ["Mail Only", "FTM", "recently sold", "Not Single Family", "Do Not Market", "Priority 2", "Priority 1"]
    ftm_x = ["recently sold", "Mail Only", "Do Not Market"]
    out = [
        ("01. HOTTEST (Prioty 1) - CALL", [("Hottest - 00 Needs Skipped", _needs("Priority 1", HOT_X)),
                                           ("Hottest - 02 Ready to Call", _call("Priority 1", 0, HOT_X))]),
        ("02. HOTTEST (Priority 1) - MAIL", [("Hottest - Mail 01 Handwritten Letter", _mail("Priority 1", 0, HOT_X))]),
        ("03. STRONG - CALL", [("Strong - 00 Needs Skipped", _needs("Priority 2", strong_x)),
                               ("Strong - 02 Ready to Call", _call("Priority 2", 0, strong_x, strong_lists)),
                               ("Strong - 03 Call Attempt 1", _call("Priority 2", 1, strong_x))]),
        ("04. STRONG - MAIL", [("Strong - Mail 01 Handwritten Letter", _mail("Priority 2", 0, strong_x))]),
        ("05. FTM - CALL", [("FTM - 00 Needs Skipped", _needs("FTM", ftm_x)),
                            ("FTM - 01 Skipped No Numbers", _needs("FTM", ftm_x, traced=1)),
                            ("FTM - 02 Ready to Call", _call("FTM", 0, ftm_x)),
                            ("FTM - 03 Call Attempt 1", _call("FTM", 1, ftm_x)),
                            ("FTM - 06 Call Attempt 4", _call("FTM", 4, ftm_x))]),
        ("06. FTM - MAIL", [("FTM - Mail 01 Handwritten Letter", _mail("FTM", 0, ftm_x)),
                            ("FTM - Mail 02 Family Postcard", _mail("FTM", 1, ftm_x))]),
        ("09. BULK - CALL", [("Bulk - 00 Needs Skipped", _needs(None, bulk_x)),
                             ("Bulk - 02 Ready to Call", _call(None, 0, bulk_x)),
                             ("Bulk - 08 Call Attempt 6", _call(None, 6, bulk_x))]),
        ("10. BULK - MAIL", [("Bulk - Mail 01 Handwritten Letter", _mail(None, 0, bulk_x))]),
        ("11. DEEP PROSPECTING (ALL TIERS)", [("02 Obituary / Deceased", {"all_lists": [_id("list", "Obituary")]})]),
        ("20. CALLER QUEUES", [("Jordan - Assigned (All)", {"assigned_to": "u-jordan"})]),
    ]
    for folder, items in (extra or {}).items():
        for f, rows in out:
            if f == folder:
                rows.extend(items)
    return out


def rec(n, owner, tags=(), lists=(), status="", phone=True, traced=True, calls=0, texts=0, mailed=0, mailed_on="",
        phones=("UNKNOWN",), vacant=False, hood="Maple Flats", fields=None):
    return {"n": n, "owner": owner, "tags": list(tags), "lists": list(lists), "status": status, "phone": phone,
            "traced": traced, "calls": calls, "texts": texts, "mailed": mailed, "mailed_on": mailed_on,
            "phones": list(phones), "vacant": vacant, "hood": hood, "fields": dict(fields or {})}


def records():
    return [
        rec(1, "o1", ("FTM",), fields={"Foreclosure Date": "2026-12-01"}),
        rec(2, "o2", ("FTM",), texts=2, fields={"Foreclosure Date": "2026-10-01"}),
        rec(3, "o3", ("FTM",), calls=1, texts=1),
        rec(4, "o4", ("FTM", "Priority 1")),
        rec(5, "o5", ("Priority 1",)),
        rec(6, "o6", ("Priority 1",), calls=2),
        rec(7, "o7", ("Priority 2",)),
        rec(8, "o8", ("Priority 2",), calls=3),
        rec(9, "o9"), rec(10, "o9"),                      # one owner, two houses: one row per owner
        rec(11, "o10", calls=5),                          # dialed five times in-house, still an Agency record
        rec(12, "o11", ("Do Not Market",)),
        rec(13, "o12", status="new_lead"),
        rec(14, "o13", ("FTM",), status="sold"),
        rec(15, "o14", phone=False, traced=False),
        rec(16, "o15", ("FTM",), fields={"Foreclosure Date": "2025-01-10"}),
        rec(17, "o16", ("Priority 2", "FTM")),
        rec(18, "o17", ("Priority 1", "Priority 2")),
        rec(19, "o18", lists=("Low Equity",)),
        rec(20, "o19", ("FTM",), mailed=1, mailed_on="2026-08-01"),
    ]


class FakeAccount:
    """Just enough of the account: the listings and the item routes the tool reads and writes, and
    a records search that counts. Like the real search it silently ignores a key it does not know."""

    def __init__(self, folders=None, recs=None):
        from datasift_api_upload import ApiError
        self.ApiError = ApiError
        self.folders, self.presets = [], {}
        for title, items in (folders if folders is not None else account()):
            fu = _id("folder", title)
            self.folders.append({"uuid": fu, "title": title, "type": "properties", "permissions": []})
            for pt, must in items:
                pu = _id("preset", pt)
                self.presets[pu] = {"uuid": pu, "title": pt, "folder": fu, "type": "properties",
                                    "quick_filter": pt.endswith("Ready to Call"),
                                    "filters": {"account": ACCOUNT, "must": copy.deepcopy(must)}}
        self.tags = {_id("tag", t): t for t in TAGS}
        self.lists = {_id("list", t): t for t in LISTS}
        self.fields = [
            {"id": 11, "uuid": _id("field", "Foreclosure Date"), "label": "Foreclosure Date", "field_type": "date",
             "entity_type": "property", "is_active": True, "machine_name": "custom_field.property.foreclosure_date"},
            {"id": 12, "uuid": _id("field", "Date Added"), "label": "Date Added", "field_type": "date",
             "entity_type": "property", "is_active": True, "machine_name": "custom_field.property.date_added"}]
        self.groups = [{"id": 1, "label": "TN Public Notice", "entity_type": "property"},
                       {"id": 2, "label": "Misc.", "entity_type": "property"}]
        self.records = records() if recs is None else recs
        self.and_any = False          # two tags in any_tags read as both at once
        self.date_shapes: set = set()  # custom date spellings the search reads
        self.date_broken = False      # a custom date filter that matches nobody, whatever it asks
        self.ignore: set = set()
        self.corrupt = ""             # a preset title whose create is stored wrongly
        self.calls, self.log, self.seq = 0, [], 0

    # ---- helpers for the checks
    def preset_by_title(self, title):
        return next((p for p in self.presets.values() if p["title"] == title), None)

    def folder_titles(self):
        return [f["title"] for f in self.folders]

    def field(self, label):
        return next((f for f in self.fields if f["label"] == label), None)

    def writes(self):
        return [x for x in self.log if x[0] in ("POST", "PATCH", "DELETE")]

    def _new(self, kind, title):
        self.seq += 1
        return _id(kind, "%s#%d" % (title, self.seq))

    def _no(self, method, path, code=404, body=""):
        return self.ApiError(code, method, path, body or '{"detail": "Not found."}')

    # ---- the routes
    def call(self, path, method="GET", body=None, headers=None):
        self.calls += 1
        self.log.append((method, path))
        base = path.split("?")[0]
        if base == "/api/internal/property/" and method == "POST" and (headers or {}).get("x-http-method-override") == "GET":
            return {"count": self.count(body["query"]["must"], counted=False), "results": []}
        if base == "/api/internal/filter-preset-folder/":
            if method == "GET":
                rows = [dict(f) for f in self.folders]
                return {"results": rows if "limit=999" in path else rows[:10]}
            if method == "POST":
                f = {"uuid": self._new("folder", body["title"]), "title": body["title"], "type": body.get("type"),
                     "permissions": body.get("permissions")}
                self.folders.append(f)
                return dict(f)
        m = re.match(r"^/api/internal/filter-preset-folder/([^/]+)/filter-preset/$", base)
        if m and method == "GET":
            rows = [copy.deepcopy(p) for p in self.presets.values() if p["folder"] == m.group(1)]
            return {"results": rows if "limit=999" in path else rows[:10]}
        m = re.match(r"^/api/internal/filter-preset-folder/([^/]+)/$", base)
        if m:
            f = next((x for x in self.folders if x["uuid"] == m.group(1)), None)
            if f is None:
                raise self._no(method, path)
            if method == "GET":
                return dict(f)
            if method == "PATCH":
                f.update({k: v for k, v in body.items() if k == "title"})
                return dict(f)
            if method == "DELETE":
                if any(p["folder"] == f["uuid"] for p in self.presets.values()):
                    raise self._no(method, path, 400, '{"detail": "The folder is not empty."}')
                self.folders.remove(f)
                return {}
        if base == "/api/internal/filter-preset/" and method == "POST":
            if any(p["title"] == body["title"] for p in self.presets.values()):
                raise self._no(method, path, 400, '{"non_field_errors": ["The fields title must make a unique set."]}')
            if not any(f["uuid"] == body["folder"] for f in self.folders):
                raise self._no(method, path, 400, '{"folder": ["Invalid folder."]}')
            pu = self._new("preset", body["title"])
            stored = copy.deepcopy(body["filters"])
            if body["title"] == self.corrupt:
                stored["must"]["phone"] = 0
            self.presets[pu] = {"uuid": pu, "title": body["title"], "folder": body["folder"], "type": body.get("type"),
                                "quick_filter": bool(body.get("quick_filter")), "filters": stored}
            return copy.deepcopy(self.presets[pu])
        m = re.match(r"^/api/internal/filter-preset/([^/]+)/$", base)
        if m:
            p = self.presets.get(m.group(1))
            if p is None:
                raise self._no(method, path)
            if method == "GET":
                return copy.deepcopy(p)
            if method == "PATCH":
                if "folder" in body:
                    p["folder"] = body["folder"]
                if "filters" in body:
                    p["filters"] = copy.deepcopy(body["filters"])
                return copy.deepcopy(p)
            if method == "DELETE":
                del self.presets[m.group(1)]
                return {}
        for kind, table, carried in (("tag", self.tags, "tags"), ("list", self.lists, "lists")):
            if base == "/api/internal/%s/" % kind:
                if method == "GET":
                    return {"results": [{"uuid": u, "title": t} for u, t in table.items()]}
                if method == "POST":
                    u = self._new(kind, body["title"])
                    table[u] = body["title"]
                    return {"uuid": u, "title": body["title"]}
            m = re.match(r"^/api/internal/%s/([^/]+)/$" % kind, base)
            if m:
                if m.group(1) not in table:
                    raise self._no(method, path)
                title = table[m.group(1)]
                n = sum(1 for r in self.records if title in r[carried])
                if method == "GET":
                    return {"uuid": m.group(1), "title": title, "properties_count": str(n)}
                if method == "DELETE":
                    if n:
                        raise self._no(method, path, 400, "")
                    del table[m.group(1)]
                    return {}
        if base == "/api/internal/custom-fields/group/" and method == "GET":
            return {"results": copy.deepcopy(self.groups)}
        if base == "/api/internal/custom-fields/":
            if method == "GET":
                return {"results": copy.deepcopy(self.fields)}
            if method == "POST":
                if not body.get("group_id"):
                    raise self._no(method, path, 400, '{"group_id": ["This field is required."]}')
                f = {"id": 100 + self.seq, "uuid": self._new("field", body["label"]), "label": body["label"],
                     "field_type": body["field_type"], "entity_type": body["entity_type"], "is_active": True,
                     "group_id": body["group_id"],
                     "machine_name": "custom_field.property." + body["label"].lower().replace(" ", "_")}
                self.fields.append(f)
                return dict(f)
        m = re.match(r"^/api/internal/custom-fields/(\d+)/$", base)
        if m:
            f = next((x for x in self.fields if x["id"] == int(m.group(1))), None)
            if f is None:
                raise self._no(method, path)
            if method == "GET":
                return dict(f)
            if method == "PATCH":
                f.update({k: v for k, v in body.items() if k == "is_active"})
                return dict(f)
        if base == "/api/internal/global-status/" and method == "GET":
            return {"results": [{"uuid": _id("global", t), "title": t, "statuses": list(s)} for t, s in PUBLISHED]}
        raise AssertionError("the fake has no route for %s %s" % (method, path))

    # ---- the search
    def count(self, must, counted=True):
        if counted:
            self.calls += 1
            self.log.append(("COUNT", ""))
        _clean(must)
        out = [r for r in self.records if self._match(r, must)]
        if must.get("show_properties") == "one_per_owner":
            return len({r["owner"] for r in out})
        return len(out)

    def _match(self, r, must) -> bool:
        for k, v in must.items():
            if k == "show_properties" or k in self.ignore:
                continue
            if k == "must_not":
                if any(self._has(r, k2, v2) is True for k2, v2 in v.items() if k2 not in self.ignore):
                    return False
            elif self._has(r, k, v) is False:
                return False
        return True

    def _in(self, r, label, w):
        x = r["fields"].get(label) or ""
        return bool(x) and (w[0] is None or x >= w[0]) and (w[1] is None or x <= w[1])

    def _has(self, r, key, v):
        if key in ("any_tags", "all_tags"):
            hits = [self.tags.get(u) in r["tags"] for u in v]
            return all(hits) if (key == "all_tags" or self.and_any) else any(hits)
        if key in ("any_lists", "all_lists"):
            hits = [self.lists.get(u) in r["lists"] for u in v]
            return all(hits) if key == "all_lists" else any(hits)
        if key == "any_property_status":
            return r["status"] in v
        if key == "any_phone_status":
            return any(p in v for p in r["phones"])
        if key == "any_neighborhood":
            return r["hood"] in v
        if key in ("phone", "skiptraced", "owner_vacant"):
            return r[{"phone": "phone", "skiptraced": "traced", "owner_vacant": "vacant"}[key]] == bool(v)
        if key in ("predictivecall_attempts", "sms_attempts", "directmail_attempts"):
            x = r[{"predictivecall_attempts": "calls", "sms_attempts": "texts", "directmail_attempts": "mailed"}[key]]
            return (v[0] is None or x >= v[0]) and (v[1] is None or x <= v[1])
        if key == "last_direct_mailed":
            x = r["mailed_on"]
            return bool(x) and (v[0] is None or x >= v[0]) and (v[1] is None or x <= v[1])
        if key == "custom_fields":
            if not isinstance(v, dict):                   # what the live search answered on 2026-11-13
                raise self.ApiError(400, "POST", "/api/internal/property/",
                                    '{"must":{"custom_fields":["Expected a dictionary of items but got type \\"list\\"."]}}')
            got = []
            for k, w in v.items():
                field = next((f for f in self.fields if (k == f.get("uuid") and "by_uuid" in self.date_shapes)
                              or (k == str(f.get("id")) and "by_id" in self.date_shapes)
                              or (k == f.get("machine_name") and "by_machine_name" in self.date_shapes)), None)
                if field:
                    got.append(False if self.date_broken else self._in(r, field["label"], w))
            return all(got) if got else None
        return None                                       # unknown: ignored, exactly like the real search


def _clean(v) -> None:
    """The search never sees a planned marker or a relative window."""
    if isinstance(v, dict):
        assert set(v) != {"$ref", "title"}, "a planned object reached the search"
        assert "ownerPropertiesOwned" not in v, "the search wants show_properties flat"
        for k, x in v.items():
            assert not str(k).startswith(("$uuid:", "$id:", "$machine:")), "a planned key reached the search"
            _clean(x)
    elif isinstance(v, list):
        for x in v:
            _clean(x)
    elif isinstance(v, str):
        assert not RELATIVE.match(v), "a relative window reached the search: %s" % v


class FakeApi:
    """The transport under the autopilot's real client, so the budget path is the production one."""

    def __init__(self, account_):
        self.account = account_

    def call(self, path, method="GET", body=None, headers=None):
        return self.account.call(path, method, body, headers)


@contextlib.contextmanager
def environ(values: dict):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _raises(fn, kind=BaseException) -> bool:
    try:
        fn()
    except kind:
        return True
    return False


def _quiet(*_a, **_k) -> None:
    return None


def run(check, tmp: Path) -> None:
    from .autopilot import preset_patch as pp
    from .autopilot import restructure as rs
    _run(check, Path(tmp) / "restructure", rs, pp)


def _run(check, tmp: Path, rs, pp) -> None:
    from . import store
    from .autopilot import budget
    from .autopilot import crm as crm_mod
    tmp.mkdir(parents=True, exist_ok=True)
    ftm, p1, p2 = _id("tag", "FTM"), _id("tag", "Priority 1"), _id("tag", "Priority 2")
    top_ref, agency_ref = {"$ref": "tag", "title": "Top 1000"}, {"$ref": "list", "title": "Agency"}

    def build(fake):
        return rs.build_plan(pp.Reader(fake, now=DAY, max_requests=100000), day="2026-11-13")

    def apply(fake, plan, name, commit=True):
        with environ({rs.ALLOW_ENV: "1"}):
            return rs.apply_plan(fake, plan, commit=commit, log_path=tmp / ("%s.jsonl" % name),
                                 backup_dir=tmp / ("backups_%s" % name), out=_quiet)

    # ------------------------------------------------------------------ the pieces
    print("  pieces")
    check(rs.canon({"any_tags": ["b", "a"], "r": [None, 5]}) == {"any_tags": ["a", "b"], "r": [None, 5]},
          "tag lists compare as sets while a range keeps its order")
    have = {("tag", "Top 1000"): "t-1",
            ("custom_field", "Sent to Agency"): {"uuid": "f-1", "machine_name": "custom_field.property.sent_to_agency"}}
    win = ["90-days", "today"]
    got = rs.resolve({"any_tags": ["x", top_ref], "must_not": {"custom_fields": {"$uuid:Sent to Agency": win,
                                                                                 "$machine:Sent to Agency": win}}}, have)
    check(got == {"any_tags": ["x", "t-1"], "must_not": {"custom_fields": {"f-1": win,
                                                                           "custom_field.property.sent_to_agency": win}}},
          "apply fills a planned object in by its id, in a value or in a key")
    check(_raises(lambda: rs.resolve({"any_lists": [agency_ref]}, {}), SystemExit)
          and _raises(lambda: rs.resolve({"custom_fields": {"$id:Sent to Agency": win}}, {}), SystemExit),
          "a preset that names an object which does not exist is never written")
    fc = rs.for_count({"any_tags": ["x", top_ref],
                       "must_not": {"any_lists": [agency_ref], "custom_fields": {"$uuid:Sent to Agency": win}}}, {}, DAY)
    check(fc == {"any_tags": ["x"]}, "counted today, an object not made yet is carried by nobody")
    check(rs.for_count({"any_lists": [agency_ref]}, {}, DAY) is None,
          "a preset that requires a list not made yet holds nobody, without asking the search")
    dated = rs.for_count({"must_not": {"custom_fields": {"$uuid:Sent to Agency": win}}}, have, DAY)
    check(dated["must_not"]["custom_fields"] == {"f-1": ["2026-08-15", "2026-11-13"]},
          "a saved 90-day window is counted with today's dates, the only form the search takes")
    check(rs.priority_must({"all_tags": ["F"], "x": 1}, "F", ["F", "T"])[0] == {"x": 1, "any_tags": ["F", "T"]},
          "an FTM anchor written as all_tags becomes FTM or Top 1000 as well")
    check(rs.priority_must({"all_tags": ["F", "A"], "any_tags": ["B"]}, "F", ["F", "T"])[0] is None,
          "an anchor that one preset cannot say is refused, never guessed")
    check(rs.clone_title("FTM - 06 Call Attempt 4") == "Priority - 06 Call Attempt 4" and rs.clone_title("X - 01") is None,
          "only a title that starts with 'FTM - ' is renamed for the Priority track")

    # ------------------------------------------------------------------ the plan
    print("  the plan")
    fake = FakeAccount()
    plan = build(fake)
    by = {p["title"]: p for p in plan["presets"]}
    check(not fake.writes(), "the plan only reads: it sends no create, change or delete")
    check(plan["blocking"] == [], "a clean account gives a plan with nothing blocking: %s" % plan["blocking"][:1])
    check(plan["proofs"]["or"]["proven"] is True, "two tags in any_tags are shown to mean either-or before the anchor is used")
    objs = {o["kind"]: o for o in plan["objects"]}
    check(all(objs[k]["action"] == "create" for k in ("tag", "list", "custom_field")),
          "the tag Top 1000, the list Agency and the field Sent to Agency are made when missing")
    check(objs["custom_field"]["body"] == {"label": "Sent to Agency", "field_type": "date", "entity_type": "property",
                                           "group_id": 2},
          "the field is a date on the property, in the Misc. group (a create needs a group)")
    check([f["title"] for f in plan["folders"]] == ["01. PRIORITY - TEXT", "02. PRIORITY - CALL", "03. PRIORITY - MAIL",
                                                     "04. AGENCY"] and all(f["action"] == "create" for f in plan["folders"]),
          "the four new folders carry the exact titles other tracks read")
    check([p["title"] for p in plan["presets"] if p["folder"] == "02. PRIORITY - CALL"]
          == ["Priority - 00 Needs Skipped", "Priority - 01 Skipped No Numbers", "Priority - 02 Ready to Call",
              "Priority - 03 Call Attempt 1", "Priority - 06 Call Attempt 4"],
          "every first-to-market call preset gets a Priority clone, 'FTM - ' renamed 'Priority - ', in order")
    check([p["title"] for p in plan["presets"] if p["folder"] == "03. PRIORITY - MAIL"]
          == ["Priority - Mail 01 Handwritten Letter", "Priority - Mail 02 Family Postcard"],
          "every first-to-market mail preset gets a Priority clone")
    src = fake.preset_by_title("FTM - 03 Call Attempt 1")["filters"]["must"]
    clone = by["Priority - 03 Call Attempt 1"]["must"]
    check(clone["any_tags"] == [ftm, top_ref], "the clone's anchor reads FTM or Top 1000, in FTM's place")
    check({k: v for k, v in clone.items() if k != "any_tags"} == {k: v for k, v in src.items() if k != "any_tags"},
          "every other key of a clone is the first-to-market preset's, exactly")
    msrc = fake.preset_by_title("FTM - Mail 02 Family Postcard")["filters"]["must"]
    check(by["Priority - Mail 02 Family Postcard"]["must"]["last_direct_mailed"] == msrc["last_direct_mailed"]
          == ["36-months", "month"], "a mail clone keeps its relative window as the app saved it")
    ready_src = fake.preset_by_title("FTM - 02 Ready to Call")["filters"]["must"]
    rt, ts = by["Priority - 01 Ready to Text"]["must"], by["Priority - 02 Texted, Not Yet Called"]["must"]
    check(rt["sms_attempts"] == [0, 0] and "predictivecall_attempts" not in rt and rt["phone"] == 1
          and rt["any_tags"] == [ftm, top_ref] and rt["must_not"] == ready_src["must_not"],
          "Ready to Text is the Priority anchor, a number, no text sent yet and the exclusions of FTM - 02 Ready to Call")
    check(ts["sms_attempts"] == [1, None] and ts["predictivecall_attempts"] == [0, 0],
          "Texted, Not Yet Called reads texted at least once and never called")
    q = by["Agency - 01 Queue"]["must"]
    check(plan["agency"]["one_preset"] is True and "Agency - 01 Queue (Strong)" not in by,
          "Strong and Bulk share one queue when their filters match once the tier tags are set aside")
    skips = q["must_not"]["any_tags"]
    check(ftm in skips and top_ref in skips and p1 not in skips and p2 not in skips and "any_tags" not in q,
          "the queue skips FTM and Top 1000 and does not route on the old tier tags")
    check(agency_ref in q["must_not"]["any_lists"], "with no date filter proven, the queue skips the whole Agency list")
    check("predictivecall_attempts" not in q and q["phone"] == 1 and q["skiptraced"] == 1
          and q.get("ownerPropertiesOwned") == {"show_properties": "one_per_owner"},
          "the queue keeps the buy box and a callable number, one row per owner, but not the call counter")
    check(by["Agency - 02 Sent"]["must"] == {"any_lists": [agency_ref]}, "Agency - 02 Sent is every record on the Agency list")
    back = by["Agency - 03 Came Back"]["must"]
    check(back["any_lists"] == [agency_ref] and set(back["any_property_status"]) == set(pp.LEAD_STATUSES),
          "Agency - 03 Came Back is the Agency list in a lead status")
    check(by["Priority - 02 Ready to Call"]["count"] == 6,
          "a Priority clone holds what its first-to-market source holds while no record carries Top 1000 (6)")
    check(by["Priority - 01 Ready to Text"]["count"] == 5 and by["Priority - 02 Texted, Not Yet Called"]["count"] == 1,
          "the text presets are counted: 5 never texted, 1 texted and not yet called")
    check(by["Agency - 01 Queue"]["count"] == 7,
          "the queue counts the buy box outside the Priority track, a record dialed in-house included (7)")
    check(by["Agency - 02 Sent"]["count"] == 0 and by["Agency - 03 Came Back"]["count"] == 0,
          "a list that does not exist yet holds nobody")
    former = {x["tag"]: x["count"] for x in plan["agency"]["former"]}
    check(former == {"Priority 1": 3, "Priority 2": 3},
          "the plan says how many old Priority 1 and Priority 2 records the queue would send: %s" % former)
    arch = plan["archive"]
    check(len(arch) == 8 and all(a["action"] == "rename" and a["new_title"] == "99. Archive - " + a["title"] for a in arch),
          "the eight tier folders are archived by renaming each one '99. Archive - <old name>'")
    check(next(a for a in arch if a["title"] == "05. FTM - CALL")["presets"][:2]
          == ["FTM - 00 Needs Skipped", "FTM - 01 Skipped No Numbers"], "an archived folder keeps every preset title")
    check(plan["proofs"]["custom_date"]["proven"] is False and plan["agency"]["window"] is None
          and "did not move" in json.dumps(plan["proofs"]["custom_date"]["rows"]),
          "a custom date filter the search does not read is not used, and each spelling tried is shown")
    check(plan["proofs"]["sms_attempts"]["proven"] is True, "the text counter is asked with and without before it is used")
    lines, md = rs.plan_lines(plan), rs.render_md(plan)
    check(any("Agency - 01 Queue" in x and "7 records" in x for x in lines) and len(lines) >= 28,
          "the plan prints every change, with the records each new preset would hold")
    check(all(ord(c) < 128 for c in md + "\n".join(lines)) and chr(0x2014) not in md and chr(0x2013) not in md,
          "the plan text is plain ASCII with no em or en dash")
    j, m = rs.write_plan(plan, tmp / "plans")
    check(j.name == "plan_2026-11-13.json" and json.loads(j.read_text(encoding="utf-8"))["tool"] == "restructure"
          and m.is_file(), "the plan is written as JSON for apply and as a page for the owner")

    # ------------------------------------------------------------------ what the plan refuses or holds
    print("  refusals in the plan")
    andfake = FakeAccount()
    andfake.and_any = True
    pa = build(andfake)
    check(pa["proofs"]["or"]["proven"] is False and any("BOTH AT ONCE" in b for b in pa["blocking"]),
          "when two tags in any_tags mean both at once, the plan is blocked and says what to do instead")
    n = len(andfake.log)
    check(_raises(lambda: apply(andfake, pa, "blocked"), SystemExit) and len(andfake.log) == n,
          "apply refuses a blocked plan without reading the account")
    pc = build(FakeAccount())
    pc["proofs"]["or"]["proven"] = None
    check(_raises(lambda: apply(FakeAccount(), pc, "unproven"), SystemExit),
          "apply refuses unless the plan proved the anchor, even with nothing else blocking")
    smsfake = FakeAccount()
    smsfake.ignore = {"sms_attempts"}
    ps = build(smsfake)
    held = [p for p in ps["presets"] if p["action"] == "held"]
    check([p["title"] for p in held] == ["Priority - 01 Ready to Text", "Priority - 02 Texted, Not Yet Called"]
          and all(p["count"] is None for p in held) and ps["blocking"] == [],
          "when the search does not read the text counter, the text presets are held back and the rest goes ahead")
    conflict = FakeAccount(account(extra={"20. CALLER QUEUES": [("Priority - 02 Ready to Call", {"phone": 1})]}))
    pk = build(conflict)
    check(any("Priority - 02 Ready to Call" in b and "different filter" in b for b in pk["blocking"]),
          "a target title someone already uses with a different filter blocks the plan (titles are account-unique)")
    stray = FakeAccount(account(extra={"05. FTM - CALL": [("Hot list - extras", {"any_tags": [ftm]})]}))
    pst = build(stray)
    check(not any(p["title"].endswith("extras") for p in pst["presets"])
          and any("Hot list - extras" in f and "not cloned" in f for f in pst["flags"]),
          "a preset in the first-to-market folder whose title does not start with 'FTM - ' is named, not cloned")
    gone = FakeAccount([x for x in account() if x[0] != "02. HOTTEST (Priority 1) - MAIL"])
    pg = build(gone)
    check(next(a for a in pg["archive"] if a["title"] == "02. HOTTEST (Priority 1) - MAIL")["action"] == "missing"
          and pg["blocking"] == [], "a tier folder that is not there is noted, not a reason to stop")
    nobulk = FakeAccount([(f, [i for i in items if i[0] != "Bulk - 02 Ready to Call"]) for f, items in account()])
    check(any("Bulk - 02 Ready to Call" in b for b in build(nobulk)["blocking"]),
          "without the Bulk Ready to Call preset there is no Agency buy box, and the plan is blocked")
    texty = FakeAccount()
    texty.fields.append({"id": 13, "uuid": _id("field", "Sent to Agency"), "label": "Sent to Agency",
                         "field_type": "text", "entity_type": "property", "is_active": True})
    check(any("not a date" in b for b in build(texty)["blocking"]),
          "a field called Sent to Agency that holds text, not a date, blocks the plan rather than being reused")
    nogroup = FakeAccount()
    nogroup.groups = []
    check(any("group" in b for b in build(nogroup)["blocking"]),
          "with no custom field group to put it in, the field cannot be made and the plan says so")
    twin = FakeAccount()
    twin.tags[_id("tag", "top 1000")] = "top 1000"
    pt = build(twin)
    check(next(o for o in pt["objects"] if o["kind"] == "tag")["action"] == "create" and any("'top 1000'" in f for f in pt["flags"]),
          "a tag that differs only in case is named and never taken for Top 1000")

    # ------------------------------------------------------------------ the date window and the split queue
    print("  the date window and a split queue")
    dfake = FakeAccount()
    dfake.date_shapes = {"by_id"}
    pd = build(dfake)
    qd = next(p for p in pd["presets"] if p["title"] == "Agency - 01 Queue")["must"]
    check(pd["proofs"]["custom_date"]["proven"] is True and pd["proofs"]["custom_date"]["shape"] == "by_id"
          and [r["verdict"] for r in pd["proofs"]["custom_date"]["rows"]] == ["did not move", "read"],
          "a custom date filter is used only once its count moves both ways, as a requirement and as an exclusion")
    check(qd["must_not"]["custom_fields"] == {"$id:Sent to Agency": ["90-days", "today"]}
          and agency_ref not in qd["must_not"].get("any_lists", []),
          "with the date filter proven, the queue skips records sent in the last 90 days instead of the whole list")
    apply(dfake, pd, "window")
    sent_id = str(dfake.field("Sent to Agency")["id"])
    check(dfake.preset_by_title("Agency - 01 Queue")["filters"]["must"]["must_not"]["custom_fields"]
          == {sent_id: ["90-days", "today"]}, "apply puts the new field's id in the window's key")
    broken = FakeAccount()
    broken.date_shapes, broken.date_broken = {"by_uuid"}, True
    pb = build(broken)
    check(pb["proofs"]["custom_date"]["proven"] is False
          and "not as an exclusion" in json.dumps(pb["proofs"]["custom_date"]["rows"]),
          "a date filter the search reads as 'nothing matches' is not taken for a working one")
    listed = FakeAccount()
    saved_shapes = rs.CUSTOM_DATE_SHAPES
    try:
        rs.CUSTOM_DATE_SHAPES = [("listed", lambda f, w: {"custom_fields": [{"field": f["uuid"], "options": list(w)}]})]
        pl = build(listed)
    finally:
        rs.CUSTOM_DATE_SHAPES = saved_shapes
    check(pl["proofs"]["custom_date"]["rows"][0]["verdict"] == "refused, or not counted"
          and pl["proofs"]["custom_date"]["proven"] is False and pl["blocking"] == [],
          "a spelling the search refuses (a list under custom_fields, as the live search did) is noted, not used")
    check("Expected a dictionary" in rs.render_md(pl), "the page quotes what the search answered when it refused")
    split = FakeAccount(account(strong_lists=BASE_LISTS + ("Obituary",)))
    psp = build(split)
    bs = {p["title"]: p for p in psp["presets"]}
    qs, qb = bs.get("Agency - 01 Queue (Strong)"), bs.get("Agency - 01 Queue (Bulk)")
    check(psp["agency"]["one_preset"] is False and qs and qb and "Agency - 01 Queue" not in bs
          and psp["agency"]["differs"] == ["must_not.any_lists"],
          "when Strong and Bulk differ, the queue is two presets and the plan names the key that differs")
    check(qs["must"]["any_tags"] == [p2] and p1 not in qs["must"]["must_not"]["any_tags"]
          and p2 in qb["must"]["must_not"]["any_tags"] and p1 not in qb["must"]["must_not"]["any_tags"]
          and all(ftm in x["must"]["must_not"]["any_tags"] for x in (qs, qb)),
          "the two queues split on Priority 2 alone and both skip the Priority track")
    check(qs["count"] + qb["count"] == 7 and any("must_not.any_lists" in f for f in psp["flags"]),
          "between them the two queues hold every Agency record once (%d and %d)" % (qs["count"], qb["count"]))

    # ------------------------------------------------------------------ apply
    print("  apply")
    fake = FakeAccount()
    plan = build(fake)
    n0 = len(fake.log)
    with environ({rs.ALLOW_ENV: None}):
        dry = rs.apply_plan(fake, plan, commit=True, out=_quiet)
    check(dry["allowed"] is False and dry["would"] and len(fake.log) == n0,
          "apply is dry without RESTRUCTURE_ALLOW=1 even with --commit, and reads nothing")
    dry2 = apply(fake, plan, "dry2", commit=False)
    check(dry2["allowed"] is False and len(fake.log) == n0, "apply is dry without --commit even with the switch set")
    before_folders = set(fake.folder_titles())
    before_presets = {(p["uuid"], p["title"]) for p in fake.presets.values()}
    done = apply(fake, plan, "main")
    backup = json.loads(Path(done["backup"]).read_text(encoding="utf-8"))
    check(Path(done["backup"]).name == "presets_ty2_2026-11-13_before_restructure.json"
          and {b["folder"]["title"] for b in backup} == before_folders
          and sum(len(b["presets"]) for b in backup) == len(before_presets),
          "apply writes a backup of every folder and preset, as they were before its first write")
    check("Top 1000" in fake.tags.values() and "Agency" in fake.lists.values(), "apply makes the tag and the list")
    field = fake.field("Sent to Agency")
    check(field and field["field_type"] == "date" and field["group_id"] == 2, "apply makes the date field in Misc.")
    titles_now = set(fake.folder_titles())
    check(all("99. Archive - " + t in titles_now for t in rs.TIER_FOLDERS) and not set(rs.TIER_FOLDERS) & titles_now,
          "the tier folders now read '99. Archive - <old name>'")
    check(before_presets <= {(p["uuid"], p["title"]) for p in fake.presets.values()},
          "no preset was deleted and no preset title changed")
    made = [p for p in fake.presets.values() if p["title"].startswith(("Priority - ", "Agency - "))]
    check(len(made) == 12, "apply makes every planned preset (%d)" % len(made))
    top = next(u for u, t in fake.tags.items() if t == "Top 1000")
    clone = fake.preset_by_title("Priority - 03 Call Attempt 1")
    call_folder = next(f["uuid"] for f in fake.folders if f["title"] == "02. PRIORITY - CALL")
    check(clone["filters"]["must"]["any_tags"] == [ftm, top] and clone["folder"] == call_folder
          and clone["filters"].get("account") == ACCOUNT,
          "after apply a clone names the real Top 1000 tag, sits in its folder and keeps its source's account")
    have_now = rs._have_now(rs.State(fake))
    check(all(fake.preset_by_title(p["title"])["filters"]["must"] == rs.resolve(p["must"], have_now)
              for p in plan["presets"]), "every preset apply made reads back exactly as planned")
    steps = [json.loads(x)["step"] for x in (tmp / "main.jsonl").read_text(encoding="utf-8").splitlines()]
    check(steps.count("preset") == 12 and steps.count("rename") == 8 and steps.count("folder") == 4
          and {"tag", "list", "field"} <= set(steps), "every write is in the log")

    print("  a second run")
    nw = len(fake.writes())
    again = apply(fake, plan, "main")
    check(len(fake.writes()) == nw and not again["made"], "a second apply changes nothing")
    check(Path(again["backup"]).name == "presets_ty2_2026-11-13_before_restructure_2.json"
          and Path(done["backup"]).is_file(), "a second run keeps the first backup and writes its own beside it")
    p2nd = build(fake)
    s = p2nd["summary"]
    check(p2nd["blocking"] == [] and s["objects_to_create"] == 0 and s["objects_to_switch_on"] == 0
          and s["folders_to_create"] == 0 and s["presets_to_create"] == 0 and s["presets_to_move"] == 0
          and s["folders_to_archive"] == 0 and s["presets_already"] == 12 and s["folders_archived"] == 8,
          "a plan after apply proposes nothing: every object, folder and preset is already in place")

    print("  a preset already made by hand")
    hand = FakeAccount()
    hand_top = _id("tag", "Top 1000")
    hand.tags[hand_top] = "Top 1000"
    want = copy.deepcopy(hand.preset_by_title("FTM - 02 Ready to Call")["filters"]["must"])
    want["any_tags"] = [ftm, hand_top]
    queue_folder = next(f["uuid"] for f in hand.folders if f["title"] == "20. CALLER QUEUES")
    hand.presets["hand-1"] = {"uuid": "hand-1", "title": "Priority - 02 Ready to Call", "folder": queue_folder,
                              "type": "properties", "quick_filter": True, "filters": {"account": ACCOUNT, "must": want}}
    ph2 = build(hand)
    entry = next(p for p in ph2["presets"] if p["title"] == "Priority - 02 Ready to Call")
    check(entry["action"] == "move" and ph2["blocking"] == [] and next(
        o for o in ph2["objects"] if o["kind"] == "tag")["action"] == "exists",
        "a same-title preset that already reads as planned counts as done, and is only moved into its folder")
    apply(hand, ph2, "hand")
    check(hand.presets["hand-1"]["folder"] == next(f["uuid"] for f in hand.folders if f["title"] == "02. PRIORITY - CALL")
          and sum(1 for p in hand.presets.values() if p["title"] == "Priority - 02 Ready to Call") == 1,
          "apply moves it rather than making a second preset of that title")
    with environ({rs.ALLOW_ENV: "1"}):
        rs.undo(hand, tmp / "hand.jsonl", commit=True, out=_quiet)
    check(hand.presets.get("hand-1", {}).get("folder") == queue_folder and hand_top in hand.tags,
          "undo puts the moved preset back in its old folder and keeps the tag it did not make")

    # ------------------------------------------------------------------ apply refuses
    print("  apply refuses")
    moved = FakeAccount()
    pm = build(moved)
    moved.preset_by_title("FTM - 03 Call Attempt 1")["filters"]["must"]["skiptraced"] = 0
    check(_raises(lambda: apply(moved, pm, "moved"), SystemExit) and not moved.writes(),
          "a source preset that changed since the plan stops apply before its first write")
    other = FakeAccount()
    po = build(other)
    po["account"] = "acct-2"
    check(_raises(lambda: apply(other, po, "other"), SystemExit) and not other.writes(),
          "a plan written for another account is refused before any write")
    bad = FakeAccount()
    pbad = build(bad)
    bad.corrupt = "Priority - 02 Ready to Call"
    check(_raises(lambda: apply(bad, pbad, "bad"), SystemExit), "a preset that reads back differently stops the run")
    check(bad.preset_by_title("Priority - 03 Call Attempt 1") is None and "05. FTM - CALL" in bad.folder_titles(),
          "nothing after the first difference is touched: no later preset, no folder renamed")

    # ------------------------------------------------------------------ undo
    print("  undo")
    with environ({rs.ALLOW_ENV: "1"}):
        rs.undo(bad, tmp / "bad.jsonl", commit=True, out=_quiet)
    check(bad.preset_by_title("Priority - 02 Ready to Call") is None,
          "undo removes a preset that read back wrongly, from the filter it stored")
    clean = FakeAccount()
    orig = (set(clean.folder_titles()), {p["title"] for p in clean.presets.values()}, set(clean.tags.values()),
            set(clean.lists.values()))
    apply(clean, build(clean), "clean")
    n = len(clean.log)
    with environ({rs.ALLOW_ENV: None}):
        d = rs.undo(clean, tmp / "clean.jsonl", commit=True, out=_quiet)
    check(not d["allowed"] and d["would"] and len(clean.log) == n, "undo is dry without both switches and touches nothing")
    with environ({rs.ALLOW_ENV: "1"}):
        d = rs.undo(clean, tmp / "clean.jsonl", commit=True, out=_quiet)
    after = (set(clean.folder_titles()), {p["title"] for p in clean.presets.values()}, set(clean.tags.values()),
             set(clean.lists.values()))
    check(after == orig and not d["left"] and not d["stopped"],
          "after apply and undo the folders, presets, tags and lists are exactly as they were")
    check(clean.field("Sent to Agency")["is_active"] is False and not any(
        x[0] == "DELETE" and "/custom-fields/" in x[1] for x in clean.log),
        "undo switches the date field off and never deletes it (the published reference has no delete for it)")
    pre = build(clean)
    check(next(o for o in pre["objects"] if o["kind"] == "custom_field")["action"] == "reactivate" and pre["blocking"] == [],
          "after an undo the plan switches the date field back on rather than making a second one")
    apply(clean, pre, "clean_again")
    check(clean.field("Sent to Agency")["is_active"] is True and len([f for f in clean.fields if f["label"] == "Sent to Agency"]) == 1,
          "apply switches the field back on, and there is still one field of that name")
    edits = FakeAccount()
    apply(edits, build(edits), "edits")
    edits.preset_by_title("Agency - 02 Sent")["filters"]["must"]["phone"] = 1
    edits.records[0]["tags"].append("Top 1000")
    with environ({rs.ALLOW_ENV: "1"}):
        d = rs.undo(edits, tmp / "edits.jsonl", commit=True, out=_quiet)
    titles = {p["title"] for p in edits.presets.values()}
    check("Agency - 02 Sent" in titles and any("edited since" in x for x in d["left"])
          and not any(t.startswith("Priority - ") for t in titles),
          "undo deletes the presets it made but leaves one someone edited since, and names it")
    check("04. AGENCY" in edits.folder_titles() and "02. PRIORITY - CALL" not in edits.folder_titles()
          and any("holds 1 preset" in x for x in d["left"]),
          "a folder that still holds a preset is left; the emptied ones are deleted")
    check("Top 1000" in edits.tags.values() and any("carry it" in x for x in d["left"]) and "Agency" not in edits.lists.values(),
          "undo leaves a tag that records now carry, and deletes an empty list it made")
    check(all("99. Archive - " not in t for t in edits.folder_titles()),
          "undo gives every tier folder its old title back")

    # ------------------------------------------------------------------ the request budget
    print("  the request budget")
    rate = {"DATASIFT_RATE_PER_MIN": "100000", "DATASIFT_RATE_NIGHT_PER_MIN": "100000",
            "DATASIFT_RATE_PER_HOUR": "1000000", "DATASIFT_RATE_NIGHT_PER_HOUR": "1000000", "DATASIFT_RATE_QUIET": "off"}

    def used(app_name: str) -> int:
        with store._LOCK, store._db() as conn:
            row = conn.execute("SELECT COALESCE(SUM(n), 0) FROM ap_rate WHERE app=? AND win LIKE 'm:%'",
                               (app_name,)).fetchone()
        return int(row[0])

    with environ(dict(rate, DATASIFT_RATE_APP="restructure_selftest_a")):
        acc = FakeAccount()
        client = crm_mod.CRM(api=FakeApi(acc), min_gap=0.0)
        start = used("restructure_selftest_a")
        planned = rs.build_plan(pp.Reader(client, now=DAY, max_requests=100000), day="2026-11-13")
        counted = used("restructure_selftest_a") - start
    check(counted == client.calls and counted > 20 and planned["blocking"] == [],
          "every request the plan makes goes through the client's shared request budget (%d counted)" % counted)
    with environ(dict(rate, DATASIFT_RATE_APP="restructure_selftest_b", DATASIFT_RATE_PER_HOUR="3",
                      DATASIFT_RATE_NIGHT_PER_HOUR="3")):
        check(_raises(lambda: rs.build_plan(pp.Reader(crm_mod.CRM(api=FakeApi(FakeAccount()), min_gap=0.0), now=DAY),
                                            day="2026-11-13"), budget.BudgetExhausted),
              "when the hourly budget is used the plan stops and says so, rather than planning from half a listing")
    whole = FakeAccount()
    pw = build(whole)
    with environ(dict(rate, DATASIFT_RATE_APP="restructure_selftest_c", **{rs.ALLOW_ENV: "1"})):
        full_client = crm_mod.CRM(api=FakeApi(whole), min_gap=0.0)
        rs.apply_plan(full_client, pw, commit=True, log_path=tmp / "whole.jsonl", backup_dir=tmp / "b_whole", out=_quiet)
    half = FakeAccount()
    ph = build(half)
    cap = str(max(10, full_client.calls // 2))
    with environ(dict(rate, DATASIFT_RATE_APP="restructure_selftest_d", DATASIFT_RATE_PER_HOUR=cap,
                      DATASIFT_RATE_NIGHT_PER_HOUR=cap, **{rs.ALLOW_ENV: "1"})):
        stopped = _raises(lambda: rs.apply_plan(crm_mod.CRM(api=FakeApi(half), min_gap=0.0), ph, commit=True,
                                                log_path=tmp / "half.jsonl", backup_dir=tmp / "b_half", out=_quiet),
                          budget.BudgetExhausted)
    with environ(dict(rate, DATASIFT_RATE_APP="restructure_selftest_e", **{rs.ALLOW_ENV: "1"})):
        rs.apply_plan(crm_mod.CRM(api=FakeApi(half), min_gap=0.0), ph, commit=True, log_path=tmp / "half.jsonl",
                      backup_dir=tmp / "b_half", out=_quiet)
    same = (sorted(half.folder_titles()) == sorted(whole.folder_titles())
            and sorted(p["title"] for p in half.presets.values()) == sorted(p["title"] for p in whole.presets.values()))
    check(stopped and same, "a run the hourly budget stops part way picks up where it stopped and ends as a whole run does")
    with environ({rs.ALLOW_ENV: "1"}):
        d = rs.undo(half, tmp / "half.jsonl", commit=True, out=_quiet)
    check(not d["left"] and not d["stopped"] and sorted(half.folder_titles()) == sorted(t for t, _ in account()),
          "undo knows every object both runs made, so the stopped and resumed apply undoes cleanly")

    # ------------------------------------------------------------------ the command line and the source
    print("  the command line and the source")
    pfile = tmp / "plans" / "plan_2026-11-13.json"
    with environ({rs.ALLOW_ENV: None}):
        code = rs.main(["apply", "--plan", str(pfile), "--commit"])
    check(code == 3, "the command line refuses --commit without RESTRUCTURE_ALLOW=1, before it builds a client")
    with environ({rs.ALLOW_ENV: None}):
        check(rs.main(["apply", "--plan", str(pfile)]) == 0 and rs.main(["undo", "--log", str(tmp / "main.jsonl")]) == 0,
              "apply and undo without --commit are dry runs that need no client")
    text = Path(rs.__file__).read_text(encoding="utf-8")
    mine = Path(__file__).read_text(encoding="utf-8")
    check(all(ord(c) < 128 for c in text + mine) and chr(0x2014) not in text + mine and chr(0x2013) not in text + mine,
          "restructure.py and its selftest are plain ASCII with no em or en dash")
    documented = {"/api/internal/filter-preset-folder/", "/api/internal/filter-preset-folder/{x}/",
                  "/api/internal/filter-preset-folder/{x}/filter-preset/", "/api/internal/filter-preset/",
                  "/api/internal/filter-preset/{x}/", "/api/internal/tag/", "/api/internal/tag/{x}/",
                  "/api/internal/list/", "/api/internal/list/{x}/", "/api/internal/custom-fields/",
                  "/api/internal/custom-fields/{x}/", "/api/internal/custom-fields/group/", "/api/internal/global-status/"}
    named = {re.sub(r"%s", "{x}", r.split("?")[0]) for r in re.findall(r'"(/api/internal/[^"]*)"', text)}
    check(named and named <= documented, "every route the tool names is one the published reference documents: %s"
          % sorted(named - documented))
