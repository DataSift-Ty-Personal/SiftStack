"""Query tools selftest: records_query, siftmap_count, activity_query and team_query, and their place in
every employee's tool list. Zero network: a fake records search that answers only the filter keys the
real one is known to honor (it IGNORES any other key, the way the real one does, and writes it down,
so a check can prove none was sent), a fake SiftMap search, and the brain's own tables on this
section's own database file, so another section's rows cannot move its counts. Every name, street,
number and id here is invented; the phone numbers sit on the never-assigned 865-000 exchange.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc)      # Friday 10:00 Eastern
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def U(n: int) -> str:
    """An invented uuid in the real 8-4-4-4-12 shape."""
    return "%08d-1111-4111-8111-%012d" % (n, n)


LISTS = {"Probate": U(901), "Foreclosure": U(902), "Obituary": U(903), "Tired Landlord": U(904)}
TAGS = [("FTM", U(911)), ("Courthouse Data", U(912)), ("Foreclosure", U(913)), ("foreclosure", U(914)),
        ("Priority 1", U(915))]
TAG = {t: u for t, u in TAGS}
ADRIANA, MARCUS, SAM_A, SAM_B = U(801), U(802), U(803), U(804)
O1, O4, O7 = U(601), U(604), U(607)


def _rec(n, street, *, lists=(), tags=(), status=None, created="2026-11-01", sold=None, assigned=None, owner=None,
         phones=False, traced=True, calls=0, texts=0, ptype="clean", year=1975):
    return {"uuid": U(n), "street": street, "city": "Exampleton", "zip": "37990", "list_titles": list(lists),
            "lists": [LISTS[t] for t in lists], "tags": [TAG[t] for t in tags], "tag_titles": list(tags),
            "status": status, "created": created, "sold": sold, "assigned": assigned, "owner": owner or U(700 + n),
            "phones": phones, "traced": traced, "calls": calls, "texts": texts, "ptype": ptype, "year": year, "n": n}


RECS = [
    _rec(1, "12 Example Ln", lists=["Probate"], created="2026-11-10", owner=O1),
    _rec(2, "14 Example Ln", lists=["Probate"], phones=True, status="not_interested", created="2026-11-12", owner=O1,
         assigned=ADRIANA),
    _rec(3, "40 Sample Rd", lists=["Foreclosure"], tags=["FTM", "Courthouse Data"], phones=True, status="new_lead",
         created="2026-10-01", sold="2021-05-02", assigned=ADRIANA, calls=2),
    _rec(4, "41 Sample Rd", lists=["Foreclosure", "Probate"], traced=False, created="2026-11-13", owner=O4,
         ptype="incomplete"),
    _rec(5, "7 Invented Way", lists=["Obituary"], tags=["FTM"], phones=True, status="Dead Lead", created="2026-09-15",
         assigned=MARCUS, calls=5, texts=1),
    _rec(6, "9 Invented Way", lists=["Foreclosure"], tags=["FTM", "Foreclosure"], phones=True, status="sold",
         sold="2026-08-20", created="2026-07-01"),
    _rec(7, "3 Placeholder Ct", lists=["Probate"], tags=["Courthouse Data"], status="prospecting", created="2026-11-01",
         owner=O7, year=1950),
    _rec(8, "5 Placeholder Ct", lists=["Probate"], tags=["foreclosure"], phones=True, created="2026-11-11", owner=O7,
         year=1955),
    _rec(9, "43 Sample Rd", lists=["Probate", "Foreclosure"], created="2026-11-05"),
    _rec(40, "2 Remote Rd", lists=["Tired Landlord"], phones=True, traced=False, status="Ice Cold", created="2026-06-01"),
] + [_rec(100 + i, "%d Filler St" % (100 + i), lists=["Probate"], created="2026-10-20") for i in range(30)]

PRESETS = [
    {"title": "Probate - 01 No Phone", "uuid": U(701), "type": "properties",
     "filters": {"must": {"any_lists": [LISTS["Probate"]], "phone": 0}}},
    {"title": "FTM - 02 Ready to Call", "uuid": U(702), "type": "properties",
     "filters": {"must": {"any_tags": [TAG["FTM"]], "phone": 1, "predictivecall_attempts": [0, 0],
                          "must_not": {"any_property_status": ["sold"]}}}},
    {"title": "Recent Adds (30d)", "uuid": U(703), "type": "properties",
     "filters": {"must": {"created": ["30-days", "today"], "any_lists": [LISTS["Probate"], LISTS["Foreclosure"]]}}},
    {"title": "Old Homes Probate", "uuid": U(704), "type": "properties",
     "filters": {"must": {"any_lists": [LISTS["Probate"]], "year": [1900, 1960],
                          "ownerPropertiesOwned": {"show_properties": "one_per_owner"}}}},
    {"title": "Broken Window", "uuid": U(705), "type": "properties",
     "filters": {"must": {"any_lists": [LISTS["Probate"]], "last_direct_mailed": ["fortnight-ish", "month"]}}},
    {"title": "Leads - Warm Pipeline", "uuid": U(706), "type": "properties",
     "filters": {"must": {"any_property_status": ["new_lead", "follow_up"]}}},
    {"title": "Assigned Pair", "uuid": U(707), "type": "properties",
     "filters": {"must": {"assigned_to": [ADRIANA, MARCUS]}}},
    {"title": "Task Board View", "uuid": U(708), "type": "tasks", "filters": {"must": {"anything": 1}}},
    {"title": "Case Twin", "uuid": U(709), "type": "properties", "filters": {"must": {"any_lists": [LISTS["Obituary"]]}}},
    {"title": "case twin", "uuid": U(710), "type": "properties", "filters": {"must": {"any_lists": [LISTS["Probate"]]}}},
]


class FakeApiError(Exception):
    """apiv2's error: the status and the raw body, as datasift_api_upload.ApiError carries them."""

    def __init__(self, code: int, body: str = ""):
        super().__init__("HTTP %s: %s" % (code, body))
        self.code, self.body = code, body


class FakeDataSift:
    """The records search as far as it is known. It answers the proven keys, IGNORES any other key the
    way the real one does and writes it down, refuses an empty filter, a page past 10,000 and a
    relative date left unconverted, matches nothing for a same-day date pair, and refuses every write."""

    KNOWN = None

    def __init__(self, records, published=("Ice Cold",)):
        from . import query
        self.KNOWN = set(query.PROVEN_KEYS) | {"year", "show_properties"}
        self.recs = [copy.deepcopy(r) for r in records]
        self.by_uuid = {r["uuid"]: r for r in self.recs}
        self.published = list(published)
        self.searches, self.gets, self.unknown, self.writes = [], [], set(), []
        self.fail = None

    def _match(self, r, must) -> bool:
        for k, v in must.items():
            if k not in self.KNOWN:
                self.unknown.add(k)
                continue
            if k == "property_type":
                ok = r["ptype"] == v
            elif k == "any_lists":
                ok = bool(set(v) & set(r["lists"]))
            elif k == "all_lists":
                ok = set(v) <= set(r["lists"])
            elif k == "any_tags":
                ok = bool(set(v) & set(r["tags"]))
            elif k == "all_tags":
                ok = set(v) <= set(r["tags"])
            elif k == "any_property_status":
                ok = r["status"] in v
            elif k in ("created", "last_sold"):
                for end in v:
                    if end is not None and not DATE_RE.match(str(end)):
                        raise FakeApiError(400, '{"%s": ["Use YYYY-MM-DD"]}' % k)
                field = r["created"] if k == "created" else r["sold"]
                ok = field is not None and (v[0] is None or field >= v[0]) and (v[1] is None or field < v[1])
            elif k == "assigned_to":
                if v is None:
                    ok = r["assigned"] is None
                elif isinstance(v, list):
                    ok = r["assigned"] in v
                else:
                    if not re.fullmatch(r"[0-9a-f-]{36}", str(v)):
                        raise FakeApiError(400, '{"assigned_to": ["Must be a valid UUID."]}')
                    ok = r["assigned"] == v
            elif k == "owners":
                ok = r["owner"] in v
            elif k == "phone":
                ok = bool(r["phones"]) == bool(v)
            elif k == "skiptraced":
                ok = bool(r["traced"]) == bool(v)
            elif k in ("predictivecall_attempts", "sms_attempts"):
                n = r["calls"] if k == "predictivecall_attempts" else r["texts"]
                ok = (v[0] is None or n >= v[0]) and (v[1] is None or n <= v[1])
            elif k == "year":
                ok = v[0] <= r["year"] <= v[1]
            elif k == "show_properties":
                ok = True
            else:   # must_not, nested inside must
                if not isinstance(v, dict):
                    raise FakeApiError(400, '{"must_not": ["Expected an object"]}')
                for kk, vv in v.items():
                    if kk == "any_lists" and set(vv) & set(r["lists"]):
                        return False
                    if kk == "any_tags" and set(vv) & set(r["tags"]):
                        return False
                    if kk == "any_property_status" and r["status"] in vv:
                        return False
                    if kk not in ("any_lists", "any_tags", "any_property_status"):
                        self.unknown.add("must_not." + kk)
                ok = True
            if not ok:
                return False
        return True

    def _row(self, r) -> dict:
        return {"uuid": r["uuid"], "address": {"street": r["street"], "city": r["city"], "zip5": r["zip"],
                                               "postal_code": r["zip"] + "-0001", "state": "TN"},
                "owner": {"uuid": r["owner"], "first_name": "Quincy", "last_name": "Testowner"},
                "status": r["status"], "list_count": len(r["lists"]) + 2, "system_list_count": 2,
                "tag_count": len(r["tags"]), "has_phones": bool(r["phones"]), "skiptraced": r["traced"],
                "phone": ({"number": "+18650000%03d" % (r["n"] % 1000), "doNotCall": True, "status": "CORRECT"}
                          if r["phones"] else None)}

    def search(self, must, limit=250, offset=0):
        self.searches.append({"must": copy.deepcopy(must), "limit": limit, "offset": offset})
        if self.fail:
            raise self.fail
        if not must:
            raise FakeApiError(400, '{"detail": "Filter can not be empty"}')
        if offset + limit > 10000:
            raise FakeApiError(400, '{"detail": "Can not fetch more than 10000 items"}')
        hits = [r for r in self.recs if self._match(r, must)]
        if must.get("show_properties") == "one_per_owner":
            seen, kept = set(), []
            for r in hits:
                if r["owner"] not in seen:
                    seen.add(r["owner"])
                    kept.append(r)
            hits = kept
        rows = [self._row(r) for r in hits]
        return len(rows), rows[offset:offset + limit]

    def call(self, path, method="GET", body=None, headers=None):
        if method != "GET" or body is not None:
            self.writes.append((method, path))
            raise AssertionError("a query tool tried to write: %s %s" % (method, path))
        self.gets.append(path)
        base = path.split("?")[0]
        if base == "/api/internal/list/":
            return {"count": len(LISTS), "results": [{"uuid": u, "title": t} for t, u in LISTS.items()]}
        if base == "/api/internal/tag/":
            return {"count": len(TAGS), "results": [{"uuid": u, "title": t} for t, u in TAGS]}
        if base == "/api/internal/filter-preset/":
            return {"count": len(PRESETS), "results": copy.deepcopy(PRESETS)}
        if base == "/api/internal/global-status/":
            return [{"title": "Lead pipeline", "statuses": list(self.published)}]
        m = re.fullmatch(r"/api/internal/property/([0-9a-f-]{36})/", base)
        if m:
            r = self.by_uuid.get(m.group(1))
            if not r:
                raise FakeApiError(404, '{"detail": "Not found."}')
            return {"uuid": r["uuid"], "address": {"street": r["street"]},
                    "owner": {"uuid": r["owner"], "first_name": "Quincy", "last_name": "Testowner"}}
        raise AssertionError("unexpected GET %s" % path)

    def count_of(self, pred) -> int:
        return sum(1 for r in self.recs if pred(r))


PROPS = [
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.00, "lon": -83.90, "sf": True, "land": False,
     "sold": "2026-07-01", "itype": None, "interfamily": False, "owner": "a", "in_account": True},
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.01, "lon": -83.91, "sf": True, "land": False,
     "sold": "2026-06-15", "itype": "flip", "interfamily": False, "owner": "b", "in_account": False},
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.02, "lon": -83.92, "sf": True, "land": False,
     "sold": "2026-02-01", "itype": "pending", "interfamily": False, "owner": "c", "in_account": False},
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.03, "lon": -83.93, "sf": True, "land": False,
     "sold": "2026-09-30", "itype": "rental", "interfamily": True, "owner": "c", "in_account": True},
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.04, "lon": -83.94, "sf": False, "land": True,
     "sold": "2026-08-01", "itype": None, "interfamily": False, "owner": "d", "in_account": False},
    {"zip": "37990", "county": "Example", "state": "TN", "lat": 36.05, "lon": -83.95, "sf": True, "land": False,
     "sold": None, "itype": None, "interfamily": False, "owner": "e", "in_account": False},
    {"zip": "37991", "county": "Example", "state": "TN", "lat": 36.20, "lon": -83.50, "sf": True, "land": False,
     "sold": "2026-10-01", "itype": "flip", "interfamily": False, "owner": "f", "in_account": True},
    {"zip": "41999", "county": "Example", "state": "KY", "lat": 37.50, "lon": -84.50, "sf": True, "land": False,
     "sold": "2026-10-02", "itype": None, "interfamily": False, "owner": "g", "in_account": False},
]


class FakeSiftMapError(RuntimeError):
    pass


FakeSiftMapError.__name__ = "SiftMapError"


class FakeMap:
    """SiftMap's search: the documented filter keys answered, any other key ignored and written down, a
    county matched on its bare name AND state (Knox County, TN in the search box counts nothing)."""

    KNOWN = {"type_single_family", "type_land", "extra_last_sale_date_min", "extra_last_sale_date_max",
             "extra_last_sale_investor_transaction_type", "extra_is_last_sale_interfamily", "extra_collapse_by_owner",
             "in_my_account_mode"}

    def __init__(self, props):
        self.props, self.bodies, self.unknown, self.fail = props, [], set(), None

    def _in_area(self, p, body) -> bool:
        for a in body.get("addresses") or []:
            if a.get("type") == "zip" and p["zip"] != a.get("search"):
                return False
            if a.get("type") == "county" and (p["county"] != a.get("search") or p["state"] != a.get("state")):
                return False
        ring = body.get("polygon")
        if ring:
            lats, lons = [x["lat"] for x in ring], [x["lon"] for x in ring]
            if not (min(lats) <= p["lat"] <= max(lats) and min(lons) <= p["lon"] <= max(lons)):
                return False
        return True

    def _ok(self, p, filters) -> bool:
        for k, v in filters.items():
            if k not in self.KNOWN:
                self.unknown.add(k)
                continue
            if k == "type_single_family" and not p["sf"]:
                return False
            if k == "type_land" and not p["land"]:
                return False
            if k == "extra_last_sale_date_min" and not (p["sold"] and p["sold"] >= v):
                return False
            if k == "extra_last_sale_date_max" and not (p["sold"] and p["sold"] <= v):
                return False
            if k == "extra_last_sale_investor_transaction_type":
                if not isinstance(v, list):
                    raise FakeSiftMapError("HTTP 400 on POST /properties/search/: expected a list")
                if p["itype"] not in v:
                    return False
            if k == "extra_is_last_sale_interfamily" and bool(p["interfamily"]) != bool(v):
                return False
            if k == "in_my_account_mode" and p["in_account"] != (v == "in"):
                return False
        return True

    def search(self, body):
        self.bodies.append(copy.deepcopy(body))
        if self.fail:
            raise self.fail
        hits = [p for p in self.props if self._in_area(p, body) and self._ok(p, body.get("filters") or {})]
        if (body.get("filters") or {}).get("extra_collapse_by_owner"):
            seen, kept = set(), []
            for p in hits:
                if p["owner"] not in seen:
                    seen.add(p["owner"])
                    kept.append(p)
            hits = kept
        return {"result_index": 0, "total_pages": 1, "page_results": 1, "total_results": len(hits),
                "data": [{"id": "9001", "address": "1 Fake St, Exampleton, TN 37990"}]}


class _Pol:
    """A playbook as far as the query tools read one: its stages and its business zone."""

    def __init__(self, stages=None, tz="America/New_York"):
        self.stages, self.tz = dict(stages or {}), tz


class NoNetwork:
    """Stands where a client stands in the checks that must make no request at all."""

    def __getattr__(self, name):
        raise AssertionError("a local query reached for the network (%s)" % name)


# ------------------------------------------------------------------------------- log events
def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _et(day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ET)


def _call(i, owner, when, kind="made", caller=None, uid="", duration=0, inbound=False, number="8650000111"):
    user = {"first_name": caller, "uuid": uid} if caller else {}
    return {"id": "qc-%s" % i, "event_type": "owner.call.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "kixie", "author": "system", "timestamp": _ts(when),
            "payload": {"call": {"destination_number": "8650000100" if inbound else number,
                                 "origin_number": number if inbound else "8650000100",
                                 "direction": "inbound" if inbound else "outbound", "duration": duration,
                                 "source": "kixie", "uuid": "call-%s" % i, "extra": {}, "user": user}}}


def _sms(i, owner, when, kind):
    inbound = kind == "received"
    return {"id": "qs-%s" % i, "event_type": "owner.sms.%s" % kind, "resource": owner, "resource_type": "owner",
            "source": "smrtphone", "author": "system", "timestamp": _ts(when),
            "payload": {"sms": {"direction": "inbound" if inbound else "outbound", "destination_number": "8650000222",
                                "origin_number": "8650000100", "status": kind, "source": "smrtphone"}}}


def _state(i, et, rec, when, payload, source="internal-api"):
    return {"id": "qx-%s" % i, "event_type": et, "resource": rec, "resource_type": "property", "source": source,
            "author": "person@example.com", "timestamp": _ts(when), "payload": payload}


def _seed_log(activity, store) -> None:
    a1, a2, a3, a4 = U(501), U(502), U(503), U(504)
    b1, b2 = U(511), U(512)
    rows = [
        # Thursday 11-12: four unnamed dials and Adriana's three answers, one inbound call, and a dial at
        # 22:00 Eastern that the log stores on Friday in UTC but belongs to Thursday's business day.
        _call(1, a1, _et(12, 14, 0)), _call(2, a2, _et(12, 14, 5)), _call(3, a3, _et(12, 14, 10)),
        _call(4, a1, _et(12, 14, 15)),
        _call(5, a1, _et(12, 14, 1), "answered", "Adriana", ADRIANA, 30),
        _call(6, a2, _et(12, 14, 6), "answered", "Adriana", ADRIANA, 75),
        _call(7, a3, _et(12, 14, 11), "answered", "Adriana", ADRIANA, 120),
        _call(8, a2, _et(12, 15, 0), "received", inbound=True),
        _call(9, a4, _et(12, 22, 0)),
        # Friday 11-13: two callers answered, so the two unnamed dials belong to nobody in particular.
        _call(10, b1, _et(13, 8, 29)), _call(11, b1, _et(13, 8, 30), "answered", "Adriana", ADRIANA, 90),
        _call(12, b2, _et(13, 8, 39)), _call(13, b2, _et(13, 8, 40), "answered", "Marcus", MARCUS, 45),
        # Weeks earlier: two different users who are both called Sam.
        _call(14, a1, _et(1, 10, 0, month=10), "answered", "Sam", SAM_A, 20),
        _call(15, a2, _et(1, 10, 5, month=10), "answered", "Sam", SAM_B, 20),
        _sms(1, a1, _et(12, 11, 0), "sent"), _sms(2, a2, _et(12, 11, 1), "sent"), _sms(3, a3, _et(12, 11, 2), "sent"),
        _sms(4, a1, _et(12, 11, 30), "received"), _sms(5, a2, _et(12, 11, 31), "failed"),
        _state(1, "property.status.updated", U(3), _et(12, 16, 0), {"property": {"status": [None, "new_lead"]}}),
        _state(2, "property.status.updated", U(5), _et(13, 9, 0), {"property": {"status": ["new_lead", "not_interested"]}},
               source="sequence"),
        _state(3, "property.status.updated", U(9), _et(13, 9, 10), {"property": {"status": [None, "new_lead"]}},
               source="bulk"),
        _state(4, "property.assigned", U(3), _et(12, 16, 5), {"property": {"assigned_to_uuid": [None, ADRIANA]}}),
        _state(5, "property.assigned", U(9), _et(13, 9, 15), {"property": {"assigned_to_uuid": [None, ADRIANA]}}),
        _state(6, "property.assigned", U(5), _et(13, 9, 20), {"property": {"assigned_to_uuid": [MARCUS, None]}}),
        _state(7, "property.tags.added", U(3), _et(12, 16, 10), {"tags": ["FTM", "Courthouse Data"]}),
        _state(8, "property.tags.added", U(6), _et(13, 9, 25), {"tags": ["FTM"]}),
        _state(9, "property.lists.added", U(1), _et(12, 16, 15), {"lists": ["Probate"]}),
        _state(10, "property.lists.added", U(2), _et(13, 9, 30), {"lists": ["Probate"]}),
    ]
    activity._store(rows)
    through = (NOW - timedelta(minutes=20)).isoformat(timespec="seconds")
    for g in ("calls", "texts", "state"):
        store.kv_set("autopilot:log:%s:through" % g, through)


def _text_of(out: dict) -> str:
    return "".join(c.get("text", "") for c in out.get("content") or [])


# ------------------------------------------------------------------------------- the section
def run(check, tmp: Path) -> None:
    from . import query, store
    saved = (config.DB_PATH, query.CRM, query.MAP, query.NOW, query.POLICY)
    env_keys = ("AGENTS_QUERY_USERS", "AGENTS_QUERY_TOOLS", "AGENTS_QUERY_INDEX_TTL_S")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    config.DB_PATH = tmp / "query_selftest.db"
    store.init()
    query.reset()
    query.NOW, query.POLICY = NOW, _Pol()      # the zone is pinned here, never read off the real playbook
    for k in env_keys:
        os.environ.pop(k, None)
    try:
        _records(check, query)
        _siftmap(check, query)
        _activity(check, query, store)
        _team(check, query, store)
        _zones(check, query)
        _tools(check, query)
        _hygiene(check)
    finally:
        config.DB_PATH, query.CRM, query.MAP, query.NOW, query.POLICY = saved
        query.reset()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _refused(query, name, args) -> str:
    """The refusal sentence, or "" when the ask was not refused."""
    try:
        query.answer(name, args)
    except query.Refused as exc:
        return str(exc)
    return ""


def _records(check, query) -> None:
    print("  records_query")
    fake = FakeDataSift(RECS)
    query.CRM, query.MAP = fake, NoNetwork()
    clean_probate_nophone = fake.count_of(lambda r: "Probate" in r["list_titles"] and not r["phones"]
                                          and r["ptype"] == "clean")

    r = query.answer("records_query", {"lists": ["Probate"], "has_phone": False})
    sent = fake.searches[-1]["must"]
    check(r["count"] == clean_probate_nophone and r["count"] == 33,
          "Probate records with no phone are counted through the search's own list and phone filters")
    check(sent == {"any_lists": [LISTS["Probate"]], "phone": 0, "property_type": "clean"},
          "the search is sent only proven keys: the list by its uuid, phone 0, and the clean record type")
    check(len(r["rows"]) == query.ROWS_DEFAULT and set(r["rows"][0]) == {"uuid", "street", "city", "zip", "status",
                                                                          "lists", "tags", "has_phones"},
          "ten example rows by default, each built from a whitelist of fields")
    out = json.dumps(r)
    check("8650000" not in out and "doNotCall" not in out and "Testowner" not in out and "Quincy" not in out,
          "no phone number, no do-not-call flag and no owner name leaves the tool")
    check(r["requests"] == 2 and r["today"] == "2026-11-13",
          "one request reads the list index and one runs the search; today is the business-local date")
    check(any("classification lists" in x for x in r["limits"]) and "clean records" in r["counted"],
          "the answer says what it counted and that a row's list count includes DataSift's own lists")
    r2 = query.answer("records_query", {"lists": ["Probate"], "has_phone": False})
    check(r2["requests"] == 1 and r2["count"] == r["count"], "the list index is kept, so the same ask costs one request")

    r = query.answer("records_query", {"lists": ["Probate"], "has_phone": False, "rows": 500})
    check(len(r["rows"]) == query.ROWS_MAX and any("rows is 0 to 25" in x for x in r["limits"])
          and fake.searches[-1]["limit"] == query.ROWS_MAX, "rows past 25 are cut to 25 and the answer says so")
    r = query.answer("records_query", {"lists": ["Probate"], "has_phone": False, "rows": 0})
    check(r["rows"] == [] and fake.searches[-1]["limit"] == 1 and r["count"] == 33, "rows=0 answers the count alone")
    r = query.answer("records_query", {"lists": ["Probate"], "has_phone": False, "include_incomplete": True})
    types = [s["must"].get("property_type") for s in fake.searches[-2:]]
    check(r["count"] == 34 and types == ["clean", "incomplete"] and "incomplete" in r["counted"],
          "include_incomplete counts the app's second tab as a second search and says both were counted")

    r = query.answer("records_query", {"statuses": ["Not Interested"]})
    check(fake.searches[-1]["must"]["any_property_status"] == ["not_interested"] and r["count"] == 1
          and r["applied"]["statuses"] == ["Not Interested"],
          "a status as the app names it is sent in the account's stored spelling")
    query.answer("records_query", {"statuses": ["New Lead", "dead lead"]})
    check(fake.searches[-1]["must"]["any_property_status"] == ["new_lead", "Dead Lead"],
          "statuses match whatever their case and spacing, and a title-case custom status keeps its spelling")
    n0, g0 = len(fake.searches), len(fake.gets)
    why = _refused(query, "records_query", {"statuses": ["Hot Potato"]})
    check("Hot Potato" in why and "Known:" in why and len(fake.searches) == n0,
          "an unknown status is refused with the known ones, and nothing is searched")
    check(any(p.startswith("/api/internal/global-status/") for p in fake.gets[g0:]),
          "an unknown status is looked for in the published lead statuses before it is refused")
    r = query.answer("records_query", {"statuses": ["ice cold"]})
    check(fake.searches[-1]["must"]["any_property_status"] == ["Ice Cold"] and r["count"] == 1,
          "a custom lead status only the account publishes is found there and sent as stored")
    from .autopilot import lanes
    query.answer("records_query", {"status_group": "leads"})
    check(fake.searches[-1]["must"]["any_property_status"] == list(lanes.LEAD_STATUS_VALUES),
          "status_group leads is every lead status the lanes know")

    r = query.answer("records_query", {"lists": ["Foreclosure"], "not_status_group": "finished"})
    must = fake.searches[-1]["must"]
    check("must_not" in must and isinstance(must["must_not"], dict)
          and must["must_not"]["any_property_status"] == list(lanes.GONE_STATUS_VALUES),
          "an exclusion is nested inside must, never sent beside it")
    check(r["count"] == 2, "excluding the finished statuses drops the sold foreclosure")
    r = query.answer("records_query", {"lists": ["Probate"], "not_tags": ["Courthouse Data"], "not_lists": ["Foreclosure"]})
    must = fake.searches[-1]["must"]
    check(must["must_not"] == {"any_tags": [TAG["Courthouse Data"]], "any_lists": [LISTS["Foreclosure"]]}
          and r["count"] == fake.count_of(lambda x: "Probate" in x["list_titles"] and "Foreclosure" not in x["list_titles"]
                                          and "Courthouse Data" not in x["tag_titles"] and x["ptype"] == "clean"),
          "list and tag exclusions ride in must_not by uuid")

    query.answer("records_query", {"lists": ["probate"]})
    check(fake.searches[-1]["must"]["any_lists"] == [LISTS["Probate"]], "a list title matches whatever its case")
    n0 = len(fake.searches)
    why = _refused(query, "records_query", {"lists": ["Probat"]})
    check("Probat" in why and "close titles: Probate" in why and len(fake.searches) == n0,
          "a list title that does not exist is refused with the close titles, never guessed, and nothing is searched")
    query.answer("records_query", {"tags": ["Foreclosure"]})
    check(fake.searches[-1]["must"]["any_tags"] == [TAG["Foreclosure"]], "a tag in its exact case is that tag")
    why = _refused(query, "records_query", {"tags": ["FORECLOSURE"]})
    check("differ only in case" in why and "'Foreclosure'" in why and "'foreclosure'" in why,
          "two tags that differ only in case are named, and neither is guessed")
    query.answer("records_query", {"tags": ["courthouse data"]})
    check(fake.searches[-1]["must"]["any_tags"] == [TAG["Courthouse Data"]], "a tag with one spelling matches any case")

    r = query.answer("records_query", {"created_from": "2026-11-10", "created_to": "2026-11-12"})
    check(fake.searches[-1]["must"]["created"] == ["2026-11-10", "2026-11-13"] and r["count"] == 3,
          "a created range is sent with the day after its last day, so the last day is counted")
    r = query.answer("records_query", {"created_from": "2026-11-12", "created_to": "2026-11-12"})
    check(fake.searches[-1]["must"]["created"] == ["2026-11-12", "2026-11-13"] and r["count"] == 1,
          "one day is never sent as a same-day pair, which matches nothing")
    r = query.answer("records_query", {"created_from": "7-days"})
    check(fake.searches[-1]["must"]["created"] == ["2026-11-06", "2026-11-14"] and r["count"] == 3,
          "a count back from today is read from the business-local date")
    r = query.answer("records_query", {"created_to": "2026-10-01"})
    check(fake.searches[-1]["must"]["created"] == [query.CREATED_FLOOR, "2026-10-02"] and r["count"] == 4,
          "a range with no start is sent with both ends")
    r = query.answer("records_query", {"sold_from": "2021-01-01", "sold_to": "2021-12-31"})
    check(fake.searches[-1]["must"]["last_sold"] == ["2021-01-01", "2022-01-01"] and r["count"] == 1,
          "the last sale range rides on last_sold")
    check("is not a date" in _refused(query, "records_query", {"created_from": "last tuesday"}),
          "a date the tool cannot read is refused with the forms it takes")
    check("is after" in _refused(query, "records_query", {"created_from": "2026-11-12", "created_to": "2026-11-10"}),
          "a range that runs backwards is refused")

    r = query.answer("records_query", {"call_attempts": "5+"})
    check(fake.searches[-1]["must"]["predictivecall_attempts"] == [5, None] and r["count"] == 1,
          "5+ is the dialer's counter from 5 up, with an open end")
    query.answer("records_query", {"call_attempts": "1-3", "text_attempts": "0"})
    check(fake.searches[-1]["must"]["predictivecall_attempts"] == [1, 3] and fake.searches[-1]["must"]["sms_attempts"] == [0, 0],
          "a range and a single count both become [low, high]")
    check("range like 1-3" in _refused(query, "records_query", {"call_attempts": "three"}), "a counter in words is refused")
    check("backwards" in _refused(query, "records_query", {"call_attempts": "3-1"}), "a counter range high to low is refused")
    r = query.answer("records_query", {"tags": ["FTM"], "has_phone": True})
    check(fake.searches[-1]["must"]["phone"] == 1 and r["count"] == 3, "has_phone true is phone 1")
    r = query.answer("records_query", {"skip_traced": False})
    check(fake.searches[-1]["must"]["skiptraced"] == 0 and r["count"] == 1, "skip_traced false is skiptraced 0")

    r = query.answer("records_query", {"assigned": "unassigned", "lists": ["Probate"]})
    m = fake.searches[-1]["must"]
    check("assigned_to" in m and m["assigned_to"] is None and r["applied"]["assigned"] == "unassigned",
          "unassigned is sent as a JSON null, the one form the search reads as nobody")
    r = query.answer("records_query", {"assigned": ADRIANA})
    check(fake.searches[-1]["must"]["assigned_to"] == ADRIANA and r["count"] == 2, "a user uuid is sent as it is")
    why = _refused(query, "records_query", {"assigned": "Adriana"})
    check("not on the Open API key" in why, "a first name nobody mapped is refused: the user list is not on the key")
    os.environ["AGENTS_QUERY_USERS"] = "Marcus=%s" % MARCUS
    try:
        r = query.answer("records_query", {"assigned": "marcus"})
        check(fake.searches[-1]["must"]["assigned_to"] == MARCUS and r["applied"]["assigned"] == "Marcus",
              "a first name set in AGENTS_QUERY_USERS resolves to that user uuid")
    finally:
        os.environ.pop("AGENTS_QUERY_USERS", None)

    r = query.answer("records_query", {"owner_of": U(2)})
    check(fake.searches[-1]["must"]["owners"] == [O1] and r["count"] == 2 and r["applied"]["owner_of"] == "14 Example Ln",
          "owner_of reads the record's owner and counts every record that owner holds")
    check("record uuid" in _refused(query, "records_query", {"owner_of": "14 Example Ln"}),
          "owner_of takes a record uuid, not a street line")
    check("No record in this account" in _refused(query, "records_query", {"owner_of": U(999)}),
          "a record uuid that is not in the account is said plainly")

    query.reset()
    g0 = len(fake.gets)
    r = query.answer("records_query", {"preset": "probate - 01 no phone"})
    preset_gets = [p for p in fake.gets[g0:] if p.startswith("/api/internal/filter-preset/")]
    check(r["applied"]["preset"] == "Probate - 01 No Phone" and r["count"] == 33 and len(preset_gets) == 1,
          "a preset by its title in any case is read from one listing request, and its filters count the same")
    query.answer("records_query", {"preset": "Probate - 01 No Phone"})
    check(len([p for p in fake.gets[g0:] if p.startswith("/api/internal/filter-preset/")]) == 1,
          "the preset listing is kept, so a second preset question reads it no more")
    r = query.answer("records_query", {"preset": "Probate - 01 No Phone", "lists": ["Foreclosure"]})
    m = fake.searches[-1]["must"]
    check(m["any_lists"] == [LISTS["Probate"]] and m["all_lists"] == [LISTS["Foreclosure"]] and r["count"] == 1,
          "one more list on a preset that already takes a list narrows it as an every-of list")
    why = _refused(query, "records_query", {"preset": "Recent Adds (30d)", "lists": ["Obituary", "Tired Landlord"]})
    check("cannot hold a second any-of list" in why,
          "two any-of lists cannot be one filter, so the ask is refused instead of answered wider")
    query.answer("records_query", {"preset": "Recent Adds (30d)"})
    check(fake.searches[-1]["must"]["created"] == ["2026-10-14", "2026-11-13"],
          "a preset's relative window is turned into dates before the search sees it")
    query.answer("records_query", {"preset": "Recent Adds (30d)", "created_from": "2026-11-01"})
    check(fake.searches[-1]["must"]["created"] == ["2026-11-01", "2026-11-13"],
          "a date range on a preset that has one is the overlap of the two")
    r = query.answer("records_query", {"preset": "Broken Window"})
    check(any("could not be read" in x and "wider" in x for x in r["limits"])
          and "last_direct_mailed" not in fake.searches[-1]["must"],
          "a preset window the tool cannot read is left out and the answer says the count is wider")
    r = query.answer("records_query", {"preset": "Old Homes Probate"})
    m = fake.searches[-1]["must"]
    check(m.get("year") == [1900, 1960] and m.get("show_properties") == "one_per_owner" and r["count"] == 1
          and any("one property per owner" in x for x in r["limits"]),
          "a preset's own filters pass through untouched, and one property per owner is said out loud")
    n0 = len(fake.searches)
    r = query.answer("records_query", {"preset": "Leads - Warm Pipeline", "statuses": ["Not Interested"]})
    check(r["count"] == 0 and len(fake.searches) == n0 and any("no search" in x for x in r["limits"]),
          "a status the preset rules out answers zero without a search, and says why")
    r = query.answer("records_query", {"preset": "Leads - Warm Pipeline", "statuses": ["New Lead"]})
    check(fake.searches[-1]["must"]["any_property_status"] == ["new_lead"] and r["count"] == 1,
          "a status inside the preset's statuses narrows to that one")
    r = query.answer("records_query", {"preset": "Assigned Pair", "assigned": ADRIANA})
    check(fake.searches[-1]["must"]["assigned_to"] == ADRIANA and r["count"] == 2,
          "one assignee of a preset that names several is sent as that one uuid")
    n0 = len(fake.searches)
    r = query.answer("records_query", {"preset": "Assigned Pair", "assigned": "unassigned"})
    check(r["count"] == 0 and len(fake.searches) == n0, "unassigned on a preset of assignees is zero with no search")
    query.answer("records_query", {"preset": "FTM - 02 Ready to Call", "not_statuses": ["Dead Lead"]})
    check(fake.searches[-1]["must"]["must_not"] == {"any_property_status": ["sold", "Dead Lead"]},
          "exclusions on a preset add to the preset's own")
    check("differ only in case" in _refused(query, "records_query", {"preset": "CASE TWIN"}),
          "two presets that differ only in case are named, and neither is guessed")
    query.answer("records_query", {"preset": "Case Twin"})
    check(fake.searches[-1]["must"]["any_lists"] == [LISTS["Obituary"]], "the exact title picks its own preset")
    why = _refused(query, "records_query", {"preset": "Probate No Phone"})
    check("No saved records preset" in why and "Probate - 01 No Phone" in why,
          "a preset title that does not exist is refused with the close titles")
    check("No saved records preset" in _refused(query, "records_query", {"preset": "Task Board View"}),
          "a preset of another type is not a records preset")

    check("siftmap_count" in _refused(query, "records_query", {"zip": "37990"}),
          "a ZIP filter is refused with where a ZIP count can be had")
    check("Use statuses" in _refused(query, "records_query", {"status": "sold"}), "status is pointed at statuses")
    why = _refused(query, "records_query", {"foo": 1})
    check("has no input 'foo'" in why and "lists" in why, "an input the tool does not have is refused, never dropped")
    r = query.answer("records_query", {})
    check(fake.searches[-1]["must"] == {"property_type": "clean"} and r["count"] == fake.count_of(lambda x: x["ptype"] == "clean"),
          "no filter at all counts every clean record, and the filter sent is never empty")

    from .autopilot import budget
    q = query._Reads(fake, cap=2)
    q.get("/api/internal/list/?offset=0&limit=1000")
    q.get("/api/internal/list/?offset=0&limit=1000")
    try:
        q.get("/api/internal/list/?offset=0&limit=1000")
        check(False, "a question stops at its request cap")
    except query.Refused as exc:
        check("more than 2" in str(exc), "a question stops at its request cap and says so")
    check(not hasattr(q, "call") and not hasattr(q, "set_status"), "the client a question sees has no way to write")
    query.reset()
    r = query.answer("records_query", {"preset": "Probate - 01 No Phone", "lists": ["Foreclosure"],
                                       "tags": ["Courthouse Data"], "statuses": ["Ice Cold"], "owner_of": U(7),
                                       "include_incomplete": True})
    check(r["requests"] == 7 and r["requests"] <= query.REQUESTS_PER_CALL,
          "the widest ask (preset, list, tag, a published status, an owner, both record types) stays inside the cap")
    fake.fail = budget.BudgetExhausted("the brain app has used its 2800 DataSift requests for this hour")
    why = _refused(query, "records_query", {"lists": ["Probate"]})
    check("share of DataSift requests" in why, "a used-up request budget is said plainly, not raised")
    fake.fail = FakeApiError(400, '{"detail": "bad filter"}')
    why = _refused(query, "records_query", {"lists": ["Probate"]})
    check("HTTP 400" in why and "bad filter" in why, "DataSift refusing a search is reported with its own words")
    fake.fail = None
    check(fake.writes == [], "no records question ever wrote")
    check(not fake.unknown, "no key the search does not honor was ever sent: %s" % sorted(fake.unknown))
    check(all(s["offset"] == 0 and s["limit"] <= query.ROWS_MAX for s in fake.searches),
          "every search reads the first page only, never near the 10,000 row wall")


def _siftmap(check, query) -> None:
    print("  siftmap_count")
    fake = FakeMap(PROPS)
    query.MAP, query.CRM = fake, NoNetwork()
    r = query.answer("siftmap_count", {"zip": "37990", "single_family": True, "sold_from": "6-months"})
    b = fake.bodies[-1]
    check(r["count"] == 3 and b["addresses"] == [{"type": "zip", "search": "37990"}]
          and b["filters"] == {"type_single_family": True, "extra_last_sale_date_min": "2026-05-13"},
          "single-family sales in a ZIP in six months: the zip, the house type and the sale date")
    check(b["size"] == 1 and b["result_index"] == 0 and "rows" not in r and "data" not in r and r["requests"] == 1,
          "a count is one request of one row, and no row comes back")
    check(any("LAST recorded sale" in x for x in r["limits"]) and any("in this account or not" in x for x in r["limits"]),
          "the answer says a sale filter reads the last sale and that the count is not only this account")
    r = query.answer("siftmap_count", {"county": "Example County, TN"})
    check(fake.bodies[-1]["addresses"] == [{"type": "county", "search": "Example", "state": "TN"}] and r["count"] == 7,
          "a county is searched by its bare name and its state, never as Example County, TN")
    check("two-letter state" in _refused(query, "siftmap_count", {"county": "Example"}), "a county needs its state")
    check("two-letter state" in _refused(query, "siftmap_count", {"county": "Example", "state": "Tennessee"}),
          "a state is two letters")
    r = query.answer("siftmap_count", {"box": "35.99,36.035,-83.935,-83.89"})
    ring = fake.bodies[-1]["polygon"]
    check(len(ring) == 5 and ring[0] == ring[-1] and set(ring[0]) == {"lat", "lon"} and r["count"] == 4,
          "a box is sent as a closed ring of lat and lon points")
    check("low before high" in _refused(query, "siftmap_count", {"box": "36.1,35.9,-84.0,-83.8"}),
          "a box with its ends swapped is refused")
    check("spans more than" in _refused(query, "siftmap_count", {"box": "-83.9,35.9,-84.0,36.1"}),
          "a box with a longitude where a latitude goes (a hemisphere tall) is refused")
    check("low before high" in _refused(query, "siftmap_count", {"box": "35.99,-83.93,36.03,-83.89"}),
          "two corners pasted as lat,lon,lat,lon are refused with the order the box takes")
    check("four numbers" in _refused(query, "siftmap_count", {"box": "1,2,3"}), "a box of three numbers is refused")
    check("never nationwide" in _refused(query, "siftmap_count", {"single_family": True}),
          "a count with no area is refused, never run nationwide")
    check("one area at a time" in _refused(query, "siftmap_count", {"zip": "37990", "county": "Example", "state": "TN"}),
          "two areas at once are refused")
    check("5-digit" in _refused(query, "siftmap_count", {"zip": "3799"}), "a ZIP of four digits is refused")
    r = query.answer("siftmap_count", {"zip": "37990-1234"})
    check(fake.bodies[-1]["addresses"][0]["search"] == "37990", "a ZIP+4 is searched as its five digits")
    r = query.answer("siftmap_count", {"zip": "37990", "investor_types": "flip, pending"})
    check(fake.bodies[-1]["filters"]["extra_last_sale_investor_transaction_type"] == ["flip", "pending"] and r["count"] == 2
          and any("pending means an investor bought" in x for x in r["limits"]),
          "investor types are sent as a list (a string 400s) and the answer explains pending and flip")
    check("takes these" in _refused(query, "siftmap_count", {"zip": "37990", "investor_types": ["flipper"]}),
          "an investor type SiftMap does not have is refused")
    r = query.answer("siftmap_count", {"zip": "37990", "exclude_family_transfers": True, "one_per_owner": True,
                                       "in_account": "not in"})
    f = fake.bodies[-1]["filters"]
    check(f == {"extra_is_last_sale_interfamily": False, "extra_collapse_by_owner": True, "in_my_account_mode": "not_in"}
          and r["count"] == 4, "family transfers left out, one per owner, and not in the account")
    r = query.answer("siftmap_count", {"zip": "37990", "one_per_owner": True})
    check(r["count"] == 5, "one per owner counts an owner of two houses in the ZIP once")
    r = query.answer("siftmap_count", {"zip": "37990", "land": True})
    check(fake.bodies[-1]["filters"] == {"type_land": True} and r["count"] == 1, "vacant land is type_land")
    check("state goes with county" in _refused(query, "siftmap_count", {"zip": "37990", "state": "TN"}),
          "a state without a county is refused")
    check("a box" in _refused(query, "siftmap_count", {"zip": "37990", "radius": "1 mile"}),
          "a radius is pointed at a box")
    fake.fail = FakeSiftMapError("HTTP 500 on POST /properties/search/: upstream")
    check("SiftMap refused the count" in _refused(query, "siftmap_count", {"zip": "37990"}),
          "a SiftMap failure is one plain sentence")
    fake.fail = None
    check(not fake.unknown, "only documented, proven SiftMap filter keys were sent: %s" % sorted(fake.unknown))
    src = (Path(__file__).resolve().parent / "query.py").read_text(encoding="utf-8")
    check("/properties/detail/" not in src and ".get_detail(" not in src and ".autocomplete(" not in src,
          "the query module never calls SiftMap detail or autocomplete (Acceptable use)")


def _activity(check, query, store) -> None:
    print("  activity_query")
    from .autopilot import activity, capture
    query.CRM = query.MAP = NoNetwork()
    r = query.answer("activity_query", {"what": "calls"})
    check(r["rows"] == [] and any("has not read the calls log" in x for x in r["limits"]),
          "before the log is read, the answer says its counts are empty, not a real zero")
    _seed_log(activity, store)

    r = query.answer("activity_query", {"what": "calls", "since": "2026-11-12", "until": "2026-11-13"})
    by = {x["caller"]: x for x in r["rows"]}
    check(r["by"] == "caller" and [x["caller"] for x in r["rows"]] == ["Adriana", "unknown", "Marcus"],
          "calls are grouped by caller by default, most dials first")
    check(by["Adriana"]["dials"] == 5 and by["Adriana"]["answered"] == 4 and by["Adriana"]["conversations"] == 3
          and by["Adriana"]["talk_minutes"] == 5.2 and by["Adriana"]["owners"] == 4,
          "a day's unnamed dials go to its only caller, and a conversation is an answer of a minute or more")
    check(by["unknown"]["dials"] == 2 and by["Marcus"]["dials"] == 0 and by["Marcus"]["answered"] == 1,
          "on a day two people called, the unnamed dials stay under unknown")
    days, _ = query._call_days(activity.events([query.DIAL, query.ANSWERED, *query.INBOUND],
                                               since=_et(12, 0), until=_et(13, 0)))
    cap = capture.day_rows(_et(12, 0), _et(13, 0), 60)
    mine = {w: {"dials": v["dials"], "answered": v["answered"], "conversations": v["conversations"],
                "talk_s": v["talk_s"], "owners": len(v["owners"] - {""})} for w, v in days["2026-11-12"].items()}
    check(mine == {w: {k: v[k] for k in ("dials", "answered", "conversations", "talk_s", "owners")}
                   for w, v in cap.items()},
          "a day's call numbers are exactly the daily capture's numbers for that day")
    r = query.answer("activity_query", {"what": "calls", "since": "2026-11-12", "until": "2026-11-13", "by": "day"})
    d = {x["day"]: x for x in r["rows"]}
    check(d["2026-11-12"]["dials"] == 5 and d["2026-11-12"]["inbound"] == 1 and d["2026-11-13"]["dials"] == 2,
          "a dial at 22:00 Eastern counts on its own business day, and inbound calls are not dials")
    r = query.answer("activity_query", {"what": "calls", "since": "2026-11-12", "until": "2026-11-13", "caller": "adriana"})
    check([x["caller"] for x in r["rows"]] == ["Adriana"], "calls can be asked for one caller by first name")
    r = query.answer("activity_query", {"what": "calls", "since": "2026-11-12", "until": "2026-11-13", "by": "total"})
    check(r["rows"][0]["dials"] == 7 and r["rows"][0]["inbound"] == 1, "a total is every dial and every inbound call")
    check(any("read through calls 2026-11-13 09:40" in x for x in r["limits"]) and any("voicemail" in x for x in r["limits"]),
          "the answer says how far the log copy is read and why an answer is not a conversation")
    r = query.answer("activity_query", {"what": "calls"})
    check(r["since"] == r["until"] == "2026-11-13", "since and until default to today")

    r = query.answer("activity_query", {"what": "texts", "since": "2026-11-12", "until": "2026-11-13"})
    check(r["rows"] == [{"day": "2026-11-12", "sent": 3, "received": 1, "failed": 1, "delivered": 0}]
          and any("Smarter Contact" in x for x in r["limits"]),
          "texts by day, with the warning that Smarter Contact texts are not in DataSift's log")
    r = query.answer("activity_query", {"what": "status_changes", "since": "2026-11-12", "until": "2026-11-13"})
    s = {x["status"]: x for x in r["rows"]}
    check(s["New Lead"]["events"] == 2 and s["New Lead"]["records"] == 2 and s["Not Interested"]["events"] == 1,
          "status changes are counted by the status they changed to, in the app's words")
    r = query.answer("activity_query", {"what": "status_changes", "since": "2026-11-12", "until": "2026-11-13",
                                        "status": "new lead"})
    check(r["events"] == 2 and [x["status"] for x in r["rows"]] == ["New Lead"], "status narrows to changes to that status")
    r = query.answer("activity_query", {"what": "status_changes", "since": "2026-11-12", "until": "2026-11-13", "by": "source"})
    check({x["source"]: x["events"] for x in r["rows"]} == {"internal-api": 1, "sequence": 1, "bulk": 1},
          "status changes can be split by what made them")
    r = query.answer("activity_query", {"what": "assignments", "since": "2026-11-12", "until": "2026-11-13"})
    check({x["assignee"]: x["events"] for x in r["rows"]} == {"Adriana": 2, "unassigned": 1},
          "assignments by assignee, the uuid named from the call log")
    r = query.answer("activity_query", {"what": "tags", "since": "2026-11-12", "until": "2026-11-13"})
    check({x["tag"]: (x["events"], x["records"]) for x in r["rows"]} == {"FTM": (2, 2), "Courthouse Data": (1, 1)},
          "tags added are counted per tag")
    r = query.answer("activity_query", {"what": "tags", "since": "2026-11-12", "until": "2026-11-13", "tag": "ftm"})
    check([x["tag"] for x in r["rows"]] == ["FTM"] and r["events"] == 2, "tag narrows to one tag")
    r = query.answer("activity_query", {"what": "lists", "since": "2026-11-12", "until": "2026-11-13"})
    check(r["rows"] == [{"list": "Probate", "events": 2, "records": 2}], "lists added are counted per list")
    r = query.answer("activity_query", {"what": "events", "since": "2026-11-12", "until": "2026-11-13"})
    types = {x["type"]: x["events"] for x in r["rows"]}
    check(types.get("owner.call.made") == 7 and types.get("property.assigned") == 3
          and any("has not read the board and notes log" in x for x in r["limits"]),
          "every stored event type can be counted, and a group never read is named")
    out = json.dumps([query.answer("activity_query", {"what": w, "since": "2026-10-01", "until": "2026-11-13"})
                      for w in query.WHAT])
    check("8650000" not in out and "call-" not in out and U(501) not in out,
          "no phone number, call id or owner id leaves the activity tool")

    check("can be grouped by" in _refused(query, "activity_query", {"what": "texts", "by": "caller"}),
          "a grouping a kind of activity does not have is refused")
    check("is one of" in _refused(query, "activity_query", {"what": "emails"}), "an activity the log copy does not keep is refused")
    check("needs what" in _refused(query, "activity_query", {}), "what is required")
    check("before since" in _refused(query, "activity_query", {"what": "calls", "since": "2026-11-13", "until": "2026-11-12"}),
          "a range that ends before it starts is refused")
    check("shorter range" in _refused(query, "activity_query", {"what": "calls", "since": "2025-01-01"}),
          "a range longer than the copy keeps is refused")
    check("goes with what=calls" in _refused(query, "activity_query", {"what": "texts", "caller": "Adriana"}),
          "a caller filter goes with calls only")
    check("never shown" in _refused(query, "activity_query", {"what": "calls", "number": "8650000111"}),
          "a phone number cannot be asked for")

    fake = FakeDataSift(RECS)
    query.CRM = fake
    query.reset()
    r = query.answer("records_query", {"assigned": "Adriana"})
    check(fake.searches[-1]["must"]["assigned_to"] == ADRIANA and r["applied"]["assigned"] == "Adriana",
          "a teammate's first name resolves to the user uuid the dialer wrote on her calls")
    check("Two DataSift users named Sam" in _refused(query, "records_query", {"assigned": "Sam"}),
          "a first name two users share is refused rather than guessed")
    query.CRM = NoNetwork()


def _team(check, query, store) -> None:
    print("  team_query")
    from .autopilot import ledger
    from .autopilot.policy import Stage
    query.CRM = query.MAP = NoNetwork()
    query.POLICY = _Pol({
        "q_assign": Stage("q_assign", "assign", "analysis", "plan", "New records handed to a caller.", ["every 30 min"], []),
        "q_score": Stage("q_score", "score", "research", "live", "Numbers scored.", ["08:30"], []),
        "q_mail": Stage("q_mail", "mail", "ops", "plan", "Letters staged.", ["10:00"], []),
        "q_off": Stage("q_off", "digest", "ops", "off", "The digest.", ["18:00"], []),
    })
    long_head = "Would assign 5 new records to Adriana. " + "More detail. " * 30
    runs = {"q_assign": {"at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds"), "live": False, "found": 22,
                         "acted": 5, "usd": 0, "would_usd": 0, "problems": ["no confirmed number, for example 12 Example Ln"],
                         "head": long_head},
            "q_score": {"at": (NOW - timedelta(hours=2)).isoformat(timespec="seconds"), "live": True, "found": 12,
                        "acted": 3, "usd": 0.05, "would_usd": 0, "problems": [], "head": "Scored 3 numbers."},
            "q_mail": {"at": (NOW - timedelta(days=1)).isoformat(timespec="seconds"), "live": False, "found": 610,
                       "acted": 31, "usd": 0, "would_usd": 58.59, "problems": [], "head": "Would mail 31 letters."},
            "q_gone": {"at": (NOW - timedelta(hours=3)).isoformat(timespec="seconds"), "live": False, "found": 1,
                       "acted": 0, "problems": [], "head": "A stage since removed from the playbook."}}
    for sid, v in runs.items():
        store.kv_set("autopilot:last:%s" % sid, json.dumps(v))
    ledger.event("run-a", "q_assign", U(1), "assign_planned", now=NOW - timedelta(hours=1))
    ledger.event("run-a", "q_assign", U(2), "assign_planned", now=NOW - timedelta(hours=1))
    ledger.event("run-b", "q_score", U(3), "scored", usd=0.015, now=NOW - timedelta(hours=2))
    staged = ledger.batch_create("q_mail", "mail", [{"record_uuid": U(1), "number": "8650000301", "street": "12 Example Ln"},
                                                    {"record_uuid": U(2), "number": "8650000302", "street": "14 Example Ln"}], 3.78)
    gone = ledger.batch_create("q_mail", "mail", [{"record_uuid": U(3), "number": "8650000303"}], 1.89)
    ledger.batch_set(gone, "released")

    r = query.answer("team_query", {"about": "stages"})
    rows = {x["stage"]: x for x in r["rows"]}
    check(set(rows) == {"q_assign", "q_score", "q_gone"}, "stages that ran today are listed by default, yesterday's are not")
    check(rows["q_assign"]["would_do"] == 5 and "did" not in rows["q_assign"] and rows["q_score"]["did"] == 3
          and rows["q_assign"]["mode"] == "plan" and rows["q_assign"]["owner"] == "Atlas",
          "a plan-mode run says what it would do, a live run what it did, under its owner's name")
    check(len(rows["q_assign"]["head"]) <= 180 and rows["q_assign"]["head"].endswith("...")
          and rows["q_assign"]["problems"] == 1, "a long headline is cut and the problems are counted")
    check(rows["q_gone"]["mode"] == "not in the playbook", "a run left by a stage the playbook no longer has is marked")
    check(r["summary"] == {"stages": 5, "ran_today": 3, "with_problems": 1, "never_run": 1, "off": 1},
          "the summary counts the playbook's stages, today's runs, problems, never run and off")
    r = query.answer("team_query", {"about": "stages", "only": "all"})
    check({x["stage"]: x["last_run"] for x in r["rows"]}["q_off"] == "never run here", "all lists a stage that never ran")
    r = query.answer("team_query", {"about": "stages", "only": "problems"})
    check([x["stage"] for x in r["rows"]] == ["q_assign"], "only=problems lists the runs that had one")
    r = query.answer("team_query", {"about": "stage", "stage": "q_assign"})
    s = r["stage"]
    check(s["problem_lines"] == ["no confirmed number, for example 12 Example Ln"] and len(s["head"]) <= 500
          and s["today_actions"] == [{"action": "assign_planned", "records": 2, "usd": 0.0}]
          and s["schedule"] == ["every 30 min"], "one stage shows its problems, today's actions and its schedule")
    r = query.answer("team_query", {"about": "stage", "stage": "q_mail"})
    check(r["stage"]["staged_batches"] == [{"batch": staged, "kind": "mail", "records": 2, "usd": 3.78,
                                           "created": r["stage"]["staged_batches"][0]["created"]}],
          "one stage shows its batches waiting for a person")
    why = _refused(query, "team_query", {"about": "stage", "stage": "q_asign"})
    check("q_assign" in why, "a mistyped stage is refused with the close ids")
    check("needs stage" in _refused(query, "team_query", {"about": "stage"}), "about=stage needs a stage")

    t_done, _ = store.enqueue("ops", "kpi_report", subject="weekly numbers", payload={"details": "call 8650000401"})
    store.finish(t_done, "done", {"ok": True})
    t_bad, _ = store.enqueue("analysis", "market_report", subject="county report")
    store.finish(t_bad, "failed", error="the county pull timed out " + "x" * 300)
    t_wait, _ = store.enqueue("sales", "ask", subject="quick screen 12 Example Ln", mode="agent")
    with store._LOCK, store._db() as c:
        c.execute("UPDATE tasks SET created=? WHERE id IN (?,?,?)", (NOW.isoformat(timespec="seconds"), t_done, t_bad, t_wait))
    r = query.answer("team_query", {"about": "tasks"})
    check(r["by_status"] == {"done": 1, "failed": 1, "queued": 1} and r["open_now"] == {"queued": 1},
          "tasks by status since today, and what is open right now")
    check(r["failed"][0]["task"] == t_bad and r["failed"][0]["who"] == "Atlas" and len(r["failed"][0]["error"]) <= 160,
          "a failed task is named with its owner and the first words of its error")
    r = query.answer("team_query", {"about": "tasks", "status": "queued"})
    check([x["task"] for x in r["listed"]] == [t_wait] and r["listed"][0]["who"] == "Roma",
          "tasks in one status are listed with their subject")
    check("8650000401" not in json.dumps(r), "a task's payload never leaves the tool")
    check("is queued, running" in _refused(query, "team_query", {"about": "tasks", "status": "lost"}),
          "a task status that does not exist is refused")

    r = query.answer("team_query", {"about": "batches"})
    check([x["batch"] for x in r["rows"]] == [staged] and r["rows"][0]["records"] == 2
          and r["by_status"] == {"staged": 1, "released": 1}, "staged batches are listed, with every status counted")
    check("8650000301" not in json.dumps(r) and "12 Example Ln" not in json.dumps(r), "a batch's items never leave the tool")
    r = query.answer("team_query", {"about": "batches", "status": "any"})
    check({x["batch"] for x in r["rows"]} == {staged, gone}, "status any lists every batch")

    store.add_spend(t_done, 0.42, "claude claude-opus-5-5")
    store.add_spend(t_bad, 0.10, "zillow sold pull")
    r = query.answer("team_query", {"about": "spend", "since": "2026-11-13"})
    check(abs(r["brain_ledger"]["usd"] - 0.52) < 1e-9
          and {x["workflow"]: x["usd"] for x in r["brain_ledger"]["by_workflow"]} == {"kpi_report": 0.42, "market_report": 0.1},
          "the brain's ledger is summed by workflow")
    check(r["autopilot_ledger"]["by_stage"] == [{"stage": "q_score", "usd": 0.015, "paid_calls": 1}],
          "the autopilot's own ledger is summed by stage")
    check("is one of" in _refused(query, "team_query", {"about": "everything"}), "an unknown report is refused")
    query.POLICY = _Pol()


def _zones(check, query) -> None:
    print("  business zone")
    query.POLICY = _Pol(tz="Pacific/Kiritimati")
    query.reset()
    check(query._today().isoformat() == "2026-11-14", "a playbook in another zone cuts today on its own clock")
    query.POLICY = _Pol(tz="Mars/Olympus_Mons")
    query.reset()
    check(query._today().isoformat() == "2026-11-13", "a zone name that does not exist falls back to Eastern")
    query.POLICY = _Pol()
    query.reset()


def _tools(check, query) -> None:
    print("  query tools in the team's tool list")
    from . import team, tools
    roster = team.load()
    ctx = tools.RunContext(task={"id": 1, "thread_id": "q-thread", "hop": 0, "slack_channel": "", "slack_thread": ""},
                           roster=roster, employee=roster.employees["sales"])
    built = tools.build(ctx)
    names = [t.name for t in built]
    i = names.index("spend_report")
    check(names[:4] == ["handoff", "ask_operator", "thread_history", "record_lookup"],
          "the team tools and record lookup still come first")
    check(names[i + 1:i + 5] == list(query.ORDER), "the four query tools follow spend report, before the workflows")
    by = {t.name: t for t in built}
    check(all(by[n].read_only and by[n].schema["additionalProperties"] is False for n in query.ORDER),
          "every query tool is read-only and takes only the inputs it names")
    for emp in roster.employees.values():
        got = [t.name for t in tools.build(tools.RunContext(task=ctx.task, roster=roster, employee=emp))]
        check(all(n in got for n in query.ORDER), "%s can ask every query" % emp.display_name)
    os.environ["AGENTS_QUERY_TOOLS"] = "0"
    try:
        check(not set(query.ORDER) & {t.name for t in tools.build(ctx)}, "AGENTS_QUERY_TOOLS=0 takes the query tools away")
    finally:
        os.environ.pop("AGENTS_QUERY_TOOLS", None)

    fake = FakeDataSift(RECS)
    query.CRM = fake
    query.reset()
    out = asyncio.run(by["records_query"].handler({"lists": ["Probate"], "has_phone": False}))
    check(not out.get("is_error") and json.loads(_text_of(out))["count"] == 33,
          "the employee's tool answers the count as JSON")
    out = asyncio.run(by["records_query"].handler({"zip": "37990"}))
    check(out.get("is_error") and _text_of(out).startswith("The records search has no ZIP"),
          "a refusal reaches the employee as its one sentence, marked as an error")
    real = query.answer
    query.answer = lambda name, args: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        out = asyncio.run(by["team_query"].handler({"about": "stages"}))
        check(out.get("is_error") and "query failed" in _text_of(out), "an unexpected error is reported, never raised")
    finally:
        query.answer = real
    query.CRM = NoNetwork()


def _hygiene(check) -> None:
    here = Path(__file__).resolve().parent
    for name in ("query.py", "selftest_query.py", "tools.py"):
        text = (here / name).read_text(encoding="utf-8")
        check(chr(0x2014) not in text and chr(0x2013) not in text and "\x08" not in text,
              "%s has no em dash, no en dash and no backspace byte" % name)
