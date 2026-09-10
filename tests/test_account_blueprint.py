"""account_blueprint: export -> blueprint -> apply, with a scripted client.

Zero network. A Stub stands in for the Client and records every call, so the
tests assert the ROUTE, the BODY and the HEADER that would go over the wire,
and the read-back comparisons that decide whether a create counts.

Why these exist: the previous clone (staging_build_39049 crm-mirror) shipped
with an empty user index (every caller queue pointed at the wrong human), a
count-only read-back (a trimmed `must` would have passed), and a cache file
that hid both. Each of those is pinned here.

Run: python tests/test_account_blueprint.py   (or pytest)
"""
import base64
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from account_blueprint import FAMILIES  # noqa: E402
from account_blueprint import apply as A  # noqa: E402
from account_blueprint import blueprint as B  # noqa: E402
from account_blueprint import client as C  # noqa: E402
from account_blueprint import export as E  # noqa: E402
from account_blueprint import translate as T  # noqa: E402
from account_blueprint.report import Log  # noqa: E402

SRC_ACCT = "bfa7e948-fab0-4819-8635-b15c3f8bcfd4"
TGT_ACCT = "e2415c08-d4f0-4b1e-9d2a-0123456789ab"
U = {  # source uuids
    "tag_p1": "11111111-1111-1111-1111-111111111111",
    "tag_rs": "22222222-2222-2222-2222-222222222222",
    "tag_junk": "33333333-3333-3333-3333-333333333333",
    "list_auction": "44444444-4444-4444-4444-444444444444",
    "list_junk": "55555555-5555-5555-5555-555555555555",
    "list_gone": "66666666-6666-6666-6666-666666666666",
    "board_lm": "77777777-7777-7777-7777-777777777777",
    "col_new": "88888888-8888-8888-8888-888888888888",
    "col_gone": "89999999-9999-9999-9999-999999999999",
    "user_adr": "9a9a9a9a-9a9a-9a9a-9a9a-9a9a9a9a9a9a",
    "tp_call": "abababab-abab-abab-abab-abababababab",
    "grp_lm": "acacacac-acac-acac-acac-acacacacacac",
    "seqf_lm": "adadadad-adad-adad-adad-adadadadadad",
}


def jwt(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "h." + body + ".s"


def good_claims(**over):
    c = {"email": "target@x.com", "account": TGT_ACCT, "user_id": "user-self-uuid",
         "exp": int(time.time()) + 48 * 3600, "feature_flags": []}
    c.update(over)
    return c


class Stub:
    """Scripted client. `script` maps (method, path) -> response | Exception |
    callable(body). GETs not in the script return an empty listing."""

    def __init__(self, script=None, kind="jwt", claims=None):
        self.script = dict(script or {})
        self.calls = []
        self.kind = kind
        self._claims = claims or {}
        self.calls_count = 0
        self._staff = None

    def claims(self):
        return dict(self._claims)

    def call(self, path, method="GET", body=None, *, base=None, method_override=None, **kw):
        self.calls.append((method, path, body, {"x-http-method-override": method_override} if method_override else {}))
        key = (method, path)
        if key in self.script:
            r = self.script[key]
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return r(body)
            return copy.deepcopy(r)
        if method == "GET":
            return {"count": 0, "results": [], "next": None}
        return {}

    def get_all(self, path, *, base=None):
        r = self.call(path, base=base)
        if isinstance(r, list):
            return r
        rows = r.get("results")
        if rows is None:
            rows = r.get("data")
        if rows is None:
            raise C.ApiError(0, "GET", path, "listing has neither results nor data")
        out = list(rows)
        nxt = r.get("next")
        while nxt:
            r = self.call(nxt, base=base)
            out.extend(r.get("results") or [])
            nxt = r.get("next")
        return out

    def search_count(self, must):
        self.calls.append(("POST", "/api/internal/property/", {"limit": 1, "offset": 0, "query": {"must": must}},
                           {"x-http-method-override": "GET"}))
        r = self.script.get(("COUNT", json.dumps(must, sort_keys=True)))
        if isinstance(r, Exception):
            raise r
        return r if r is not None else 0

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PATCH", "DELETE")
                and not (c[1] == "/api/internal/property/" and c[3])]


def rows(*items):
    return {"count": len(items), "results": list(items), "next": None}


# --------------------------------------------------------------- source index

def source_index():
    idx = T.SourceIndex()
    idx.lists = {U["list_auction"]: "Auction", U["list_junk"]: "744e99ae-f140-458a-beed-38a17700acb8"}
    idx.tags = {U["tag_p1"]: "Priority 1", U["tag_rs"]: "recently sold", U["tag_junk"]: "2026-W27"}
    idx.boards = {U["board_lm"]: "Lead Management"}
    idx.columns = {U["col_new"]: ("Lead Management", "New Lead (Unqualified)")}
    idx.users = {U["user_adr"]: "Adriana"}
    idx.task_presets = {U["tp_call"]: ("Lead Management", "Call New Lead")}
    idx.task_groups = {U["grp_lm"]: "Lead Management"}
    idx.sequence_folders = {U["seqf_lm"]: "Lead Management"}
    return idx


# ================================================================ export side

def test_preset_refs_dedupe_drop_and_account_removed():
    log = Log()
    filt = {"account": SRC_ACCT,
            "must": {"any_tags": [U["tag_p1"], U["tag_p1"]], "phone": 1,
                     "assigned_to": U["user_adr"],
                     "must_not": {"any_lists": [U["list_auction"], U["list_auction"], U["list_gone"], U["list_junk"]],
                                  "any_neighborhood": ["Farrport"]}}}
    out = T.preset_filters_to_refs(filt, source_index(), log, "P")
    assert "account" not in out, "filters.account must never reach the blueprint"
    assert out["must"]["any_tags"] == [B.ref("tag", "Priority 1")], "duplicated uuids collapse to one ref"
    assert out["must"]["must_not"]["any_lists"] == [B.ref("list", "Auction")], "gone + junk lists dropped, dup deduped"
    assert out["must"]["assigned_to"] == B.ref("user", "Adriana")
    assert out["must"]["must_not"]["any_neighborhood"] == ["Farrport"], "market strings pass through export"
    dropped = [e for e in log.entries if e["action"] == "dropped"]
    assert len(dropped) == 2 and "junk" in dropped[1]["detail"], "junk list is DROPPED, never remapped to Auction"


def test_sniffer_catches_unknown_key_and_unresolved():
    log = Log()
    node = {"weird_key": U["col_new"], "gone": "deadbeef-dead-dead-dead-deaddeaddead",
            "not_uuid": "1757520000000"}
    out = T.sniff(node, source_index(), log, "sequences", "S", "x")
    assert out["weird_key"] == B.ref("column", "New Lead (Unqualified)", board="Lead Management")
    assert B.is_unresolved(out["gone"]), "an unknown uuid becomes $unresolved, never copied"
    assert out["not_uuid"] == "1757520000000", "a timestamp string is not a uuid"


def test_sequence_refs_strip_meta_manual_actions_and_end_of_day():
    log = Log()
    seq = {"uuid": "deadbeef-dead-dead-dead-deaddeaddead", "created": "x", "runs": 3,
           "created_by": {"email": "ty+2@dataflik.com"}, "account_uuid": SRC_ACCT,
           "title": "Call New Lead", "is_active": True, "trigger": "property.status.updated",
           "folder": {"uuid": U["seqf_lm"], "title": "Lead Management"},
           "conditions": [{"condition": "has_all", "payload": {"field": "tags_uuid", "values": [U["tag_rs"]], "resource": True}},
                          {"condition": "from_to", "payload": {"field": "column", "meta": {"boards": [U["board_lm"]]},
                                                               "updated_to": U["col_new"]}}],
           "actions": [{"action": "create-task-by-preset",
                        "payload": {"task_preset": U["tp_call"], "end_of_day": "2026-03-04T04:59:59.999Z",
                                    "timezone": "America/New_York", "uuid": "1757520000000"}},
                       {"action": "send-sms", "payload": {"origin_number": "+18653241736",
                                                          "send_to_custom": ["865-951-8118"], "message": "New Lead"}},
                       {"action": "send-email", "payload": {"integration_uuid": "1f5a3fcd-d162-45a4-90f4-d1efa4fe7e49",
                                                            "send_to_custom": ["rami@volunteerhomebuyers.com"]}},
                       {"action": "property-assign", "payload": {"field": "assigned_to", "value": U["user_adr"]}}]}
    out = T.sequence_to_refs(seq, source_index(), log)
    for k in ("uuid", "created", "runs", "created_by", "account_uuid"):
        assert k not in out
    assert out["folder"] == "Lead Management"
    assert out["conditions"][0]["payload"]["values"] == [B.ref("tag", "recently sold")]
    assert out["conditions"][1]["payload"]["meta"]["boards"] == [B.ref("board", "Lead Management")]
    assert out["conditions"][1]["payload"]["updated_to"] == B.ref("column", "New Lead (Unqualified)", board="Lead Management")
    a0 = out["actions"][0]["payload"]
    assert a0["task_preset"] == B.ref("task_preset", "Call New Lead", group="Lead Management")
    assert "end_of_day" not in a0 and a0["timezone"] == "America/New_York"
    assert out["actions"][1].get("$manual") and out["actions"][2].get("$manual")
    assert "origin_number" not in json.dumps(out) and "rami@" not in json.dumps(out)
    assert out["actions"][3]["payload"]["value"] == B.ref("user", "Adriana")
    assert len(out["manual"]) == 2
    text = json.dumps(out)
    assert "deadbeef" not in text and SRC_ACCT not in text


def test_leak_scan_refuses_phone_email_uuid():
    bp = B.empty()
    bp["source"]["account_uuid"] = SRC_ACCT
    bp["sequences"] = [{"title": "S", "folder": None, "actions": [{"payload": {"x": "865-951-8118"}}]}]
    assert any("phone" in p for p in B.scan_leaks(bp))
    bp["sequences"] = [{"title": "S", "actions": [{"payload": {"x": "rami@volunteerhomebuyers.com"}}]}]
    assert any("email" in p for p in B.scan_leaks(bp))
    bp["sequences"] = [{"title": "S", "actions": [{"payload": {"x": U["col_new"]}}]}]
    assert any("uuid" in p for p in B.scan_leaks(bp))
    bp["sequences"] = [{"title": "S", "actions": [{"payload": {"x": {"$unresolved": U["col_new"], "path": "p"}}}]}]
    assert B.scan_leaks(bp) == [], "$unresolved markers are allowed (apply treats them as gaps)"


def test_validate_catches_missing_color_junk_tag_empty_must_and_account():
    bp = B.empty()
    bp["source"]["account_uuid"] = SRC_ACCT
    bp["statuses"] = [{"title": "Hot Lead", "color": None, "system": False}]
    bp["tags"] = [{"title": "2026-W27", "why": ["x"]}]
    bp["preset_folders"] = [{"title": "01. A", "numbered": True, "presets": [
        {"title": "P", "filters": {"account": SRC_ACCT, "must": {}}},
        {"title": "P", "filters": {"must": {"any_tags": [B.ref("tag", "Missing")]}}}]}]
    errors, _ = B.validate(bp)
    text = "\n".join(errors)
    for needle in ("no color", "junk tag", "empty must", "filters.account", "duplicate preset", "not present"):
        assert needle in text, needle


def fake_export_client():
    """A live-shaped source account as the export would read it."""
    s = {}
    s[("GET", "/api/internal/status/?limit=1000")] = rows(
        {"uuid": "s1", "title": "new_lead", "color": "#7431d7", "is_active": True, "created_by": None, "order": 1},
        {"uuid": "s2", "title": "Hot Lead", "color": "#ff0000", "is_active": True, "created_by": {"x": 1}, "order": 5})
    s[("GET", "/api/internal/list/?limit=999")] = rows(
        {"uuid": U["list_auction"], "title": "Auction"}, {"uuid": U["list_junk"], "title": "744e99ae-f140-458a-beed-38a17700acb8"})
    s[("GET", "/api/internal/tag/?offset=0&limit=10000&ordering=title")] = rows(
        {"uuid": U["tag_p1"], "title": "Priority 1"}, {"uuid": U["tag_rs"], "title": "recently sold"},
        {"uuid": U["tag_junk"], "title": "2026-W27"}, {"uuid": "t4", "title": "Buy Box - Knox"},
        {"uuid": "t5", "title": "pulled_2026-08-20"}, {"uuid": "t6", "title": "Courthouse Data, code_violation, Knox"})
    s[("GET", "/api/internal/account/user/?offset=0&limit=999")] = [
        {"uuid": U["user_adr"], "first_name": "Adriana", "last_name": "M", "role": "prospector", "is_active": True,
         "email": "adriana@x.com"}]
    s[("GET", "/api/internal/custom-fields/group/")] = rows({"id": 294, "label": "TN Public Notice", "entity_type": "property"})
    s[("GET", "/api/internal/custom-fields/?limit=999")] = rows(
        {"id": 1, "uuid": "cf-1", "label": "Notice Type", "field_type": "select", "entity_type": "property",
         "group": {"id": 294, "label": "TN Public Notice"}, "options": [{"label": "lien", "is_active": True}]})
    s[("GET", "/api/internal/task-group/?offset=0&limit=999")] = rows({"uuid": U["grp_lm"], "title": "Lead Management"})
    s[("GET", "/api/internal/task-group/%s/task-preset/?offset=0&limit=999" % U["grp_lm"])] = rows(
        {"uuid": U["tp_call"], "title": "Call New Lead", "round_robin": True, "expires_in": {"times": "1", "period": "day"},
         "all_day": False, "due_time": "02:00:00", "assigned_to_user": {"uuid": U["user_adr"], "first_name": "Adriana"},
         "assigned_to_users": [U["user_adr"]], "order": 1, "skip_weekends": False})
    s[("GET", "/api/internal/siftline/board/?offset=0&limit=999")] = rows({"uuid": U["board_lm"], "title": "Lead Management"})
    s[("GET", "/api/internal/siftline/board/%s/column/?offset=0&limit=999" % U["board_lm"])] = rows(
        {"uuid": U["col_new"], "title": "New Lead (Unqualified)", "order": 0})
    s[("GET", "/api/internal/sequence-folder/?limit=999")] = rows({"uuid": U["seqf_lm"], "title": "Lead Management"})
    s[("GET", "/api/internal/sequence/?limit=999")] = rows(
        {"uuid": "q1", "title": "Recently Sold to Sold Status", "is_active": True, "account_uuid": SRC_ACCT,
         "created_by": {"email": "ty+2@dataflik.com"}, "folder": {"uuid": U["seqf_lm"], "title": "Lead Management"},
         "trigger": "property.tags.added",
         "conditions": [{"condition": "has_all", "payload": {"field": "tags_uuid", "values": [U["tag_rs"]], "resource": True}}],
         "actions": [{"action": "set-field-value", "payload": {"field": "status", "value": "sold"}},
                     {"action": "remove", "payload": {"tags": ["Priority 1", "2026-W27", "pulled_2026-08-20"], "lists": ["Auction"]}}]})
    s[("GET", "/api/internal/filter-preset-folder/?type=properties&limit=999")] = rows(
        {"uuid": "f1", "title": "01. HOTTEST - CALL", "type": "properties"}, {"uuid": "f2", "title": "default", "type": "properties"})
    s[("GET", "/api/internal/filter-preset-folder/f1/filter-preset/?limit=999")] = rows({"uuid": "p1", "title": "Hottest - 02 Ready to Call"})
    s[("GET", "/api/internal/filter-preset-folder/f2/filter-preset/?limit=999")] = rows({"uuid": "p2", "title": "00. Needs Skipped"})
    s[("GET", "/api/internal/filter-preset/p1/")] = {
        "uuid": "p1", "title": "Hottest - 02 Ready to Call", "quick_filter": True,
        "filters": {"account": SRC_ACCT, "must": {"any_tags": [U["tag_p1"]], "phone": 1, "predictivecall_attempts": [0, 0],
                                                 "ownerPropertiesOwned": {"show_properties": "one_per_owner"},
                                                 "last_direct_mailed": ["36-months", "month"],
                                                 "must_not": {"any_lists": [U["list_auction"]], "any_neighborhood": ["Farrport"]}}}}
    s[("GET", "/api/internal/filter-preset/p2/")] = {
        "uuid": "p2", "title": "00. Needs Skipped", "quick_filter": False,
        "filters": {"account": SRC_ACCT, "must": {"skiptraced": 0}}}
    s[("GET", "/filters/?scope=account&page_size=100&page=1")] = {
        "count": 2, "next": "https://map.reisift.io/filters/?scope=account&page_size=100&page=2",
        "results": [{"id": 1, "name": "Knox - Tier 2 - Vacant", "is_active": True, "auto_add_enabled": True,
                     "replace_owners_enabled": False, "lists": [], "tags": ["Priority 1"],
                     "filter_data": {"filters": {"x": 1}, "addresses": [{"county": "Knox", "title": "Knox County, TN"}]}}]}
    s[("GET", "https://map.reisift.io/filters/?scope=account&page_size=100&page=2")] = {
        "count": 2, "next": None,
        "results": [{"id": 2, "name": "old", "is_active": False, "filter_data": {}}]}
    s[("COUNT", json.dumps({"any_tags": [U["tag_p1"]], "phone": 1, "predictivecall_attempts": [0, 0], "show_properties": "one_per_owner",
                            "must_not": {"any_lists": [U["list_auction"]], "any_neighborhood": ["Farrport"]}}, sort_keys=True))] = 72
    return Stub(s, kind="api_key")


def test_export_end_to_end_shape():
    c = fake_export_client()
    bp = E.Exporter(c, with_counts=True, label="t", email="ty+2@dataflik.com").run()
    errors, warnings = B.validate(bp)
    assert errors == [], errors
    assert bp["source"]["account_uuid"] == SRC_ACCT, "source account derived from the presets' own account key"
    assert [u["first_name"] for u in bp["users"]] == ["Adriana"], "users come back from a FLAT ARRAY"
    assert "last_name" not in bp["users"][0], "staff last names never enter the public file"
    assert "email" not in bp["users"][0]
    folders = {f["title"]: f for f in bp["preset_folders"]}
    assert folders["default"]["numbered"] is False and folders["01. HOTTEST - CALL"]["numbered"] is True
    p = folders["01. HOTTEST - CALL"]["presets"][0]
    assert p["source_count"] == 72 and p["source_count_approx"] is True, "relative date window dropped for the count"
    assert p["filters"]["must"]["any_tags"] == [B.ref("tag", "Priority 1")]
    tags = {t["title"]: t["why"] for t in bp["tags"]}
    assert set(tags) == {"Priority 1", "recently sold", "Buy Box - Knox"}, tags
    assert "anchor" in tags["Buy Box - Knox"] and any(w.startswith("preset:") for w in tags["Priority 1"])
    assert [x["title"] for x in bp["lists"]] == ["Auction"], "uuid-titled list not exported"
    seq = bp["sequences"][0]
    assert seq["actions"][1]["payload"]["tags"] == ["Priority 1"], "remove-action values filtered to shipped tags"
    assert [m["name"] for m in bp["siftmap_presets"]] == ["Knox - Tier 2 - Vacant"], "next followed, inactive skipped"
    assert bp["siftmap_presets"][0]["counties"] == ["Knox"]
    tp = bp["task_presets"][0]
    assert tp["assigned_to_user"] == B.ref("user", "Adriana") and tp["assigned_to_users"] == [B.ref("user", "Adriana")]
    assert bp["custom_fields"][0]["group"] == "TN Public Notice"
    assert bp["statuses"][0]["system"] is True and bp["statuses"][1]["system"] is False
    # the ONLY uuid in the file is the source account
    assert B.scan_leaks(bp) == []


def test_export_writes_nothing():
    c = fake_export_client()
    E.Exporter(c, with_counts=False).run()
    assert c.writes() == [], "export is read-only"
    assert all(h.get("x-http-method-override") == "GET" for m, p, b, h in c.calls if p == "/api/internal/property/")


# ================================================================= apply side

def min_blueprint():
    bp = B.empty()
    bp["source"] = {"label": "t", "email": "ty+2@dataflik.com", "account_uuid": SRC_ACCT, "auth_by_family": {}}
    bp["statuses"] = [{"title": "new_lead", "color": "#7431d7", "is_active": True, "order": 1, "system": True},
                      {"title": "Hot Lead", "color": "#ff0000", "is_active": True, "order": 5, "system": False}]
    bp["lists"] = [{"title": "Auction"}]
    bp["tags"] = [{"title": "Priority 1", "why": ["preset:P"]}]
    bp["boards"] = [{"title": "Lead Management", "columns": ["New Lead (Unqualified)"]}]
    bp["users"] = [{"first_name": "Adriana", "last_name": "M", "role": "prospector", "is_active": True},
                   {"first_name": "Tinaa", "last_name": "G", "role": "prospector", "is_active": True}]
    bp["task_groups"] = [{"title": "Lead Management"}]
    bp["task_presets"] = [{"group": "Lead Management", "title": "Call New Lead", "notes": None, "round_robin": True,
                           "expires_in": {"times": "1", "period": "day"}, "all_day": False, "due_time": "02:00:00",
                           "assigned_to_user": B.ref("user", "Adriana"), "assigned_to_users": [], "assigned_to_role": None,
                           "order": 1, "skip_weekends": False}]
    bp["sequence_folders"] = [{"title": "Lead Management"}]
    bp["sequences"] = [{"title": "Sold Tag", "folder": "Lead Management", "is_active": True, "trigger": "property.tags.added",
                        "conditions": [{"condition": "has_all", "payload": {"field": "tags_uuid",
                                                                            "values": [B.ref("tag", "Priority 1")], "resource": True}}],
                        "actions": [{"action": "set-field-value", "payload": {"field": "status", "value": "sold"}}]}]
    bp["preset_folders"] = [{"title": "01. HOTTEST - CALL", "type": "properties", "numbered": True, "presets": [
        {"title": "Hottest - 02 Ready to Call", "quick_filter": True, "source_count": 72,
         "filters": {"must": {"any_tags": [B.ref("tag", "Priority 1")], "phone": 1,
                              "assigned_to": B.ref("user", "Tinaa"),
                              "must_not": {"any_lists": [B.ref("list", "Auction")], "any_neighborhood": ["Farrport"],
                                           "any_property_status": ["Cold Lead"]}}}}]},
        {"title": "default", "type": "properties", "numbered": False, "presets": [
            {"title": "Legacy", "quick_filter": False, "source_count": 0, "filters": {"must": {"skiptraced": 0}}}]}]
    bp["siftmap_presets"] = [{"name": "Knox - Vacant", "description": "", "auto_add_enabled": True,
                              "replace_owners_enabled": False, "email_enabled": False, "lists": [], "tags": ["Priority 1"],
                              "filter_data": {"filters": {"x": 1}, "addresses": [{"county": "Knox"}]}, "counties": ["Knox"]}]
    assert B.validate(bp)[0] == [], B.validate(bp)[0]
    return bp


def target_script(**over):
    """An EMPTY target account with the default boards and system statuses."""
    s = {}
    s[("GET", "/api/internal/status/?limit=1000")] = rows(
        {"uuid": "ts1", "title": "new_lead", "color": "#7431d7", "is_active": True, "created_by": None, "order": 1},
        {"uuid": "ts2", "title": "cold lead", "color": "#000", "is_active": True, "created_by": {"x": 1}, "order": 3})
    s[("GET", "/api/internal/list/?limit=999")] = rows()
    s[("GET", "/api/internal/tag/?offset=0&limit=10000&ordering=title")] = rows()
    s[("GET", "/api/internal/siftline/board/?offset=0&limit=999")] = rows({"uuid": "tb1", "title": "Lead Management"})
    s[("GET", "/api/internal/siftline/board/tb1/column/?offset=0&limit=999")] = rows({"uuid": "tc1", "title": "New Lead (Unqualified)"})
    s[("GET", "/api/internal/account/user/?offset=0&limit=999")] = [
        {"uuid": "user-self-uuid", "first_name": "Target", "last_name": "Owner", "is_active": True},
        {"uuid": "user-jane", "first_name": "Jane", "last_name": "D", "is_active": True}]
    s[("GET", "/api/internal/task-group/?offset=0&limit=999")] = rows()
    s[("GET", "/api/internal/sequence-folder/?limit=999")] = rows()
    s[("GET", "/api/internal/sequence/?limit=999")] = rows()
    s[("GET", "/api/internal/sequence/?limit=5")] = rows()
    s[("GET", "/api/internal/custom-fields/?limit=999")] = rows()
    s[("GET", "/api/internal/custom-fields/group/")] = rows({"id": 9, "label": "Misc."})
    s[("GET", "/api/internal/filter-preset-folder/?type=properties&limit=999")] = rows()
    s[("GET", "/filters/?scope=account&page_size=100&page=1")] = rows()
    s[("POST", "/api/internal/upload/usage/")] = {"upload_usage": 0, "upload_limit": 100000}
    s.update(over)
    return s


def opts(**kw):
    o = A.Options(blueprint_path="", state_path="", report_path="")
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def run(bp, client, **kw):
    return A.run_apply(client, bp, "target@x.com", opts(**kw))


# ---- the gate

def test_gate_refuses_wrong_email():
    c = Stub(target_script(), claims=good_claims(email="other@x.com"))
    try:
        run(min_blueprint(), c)
        assert False
    except SystemExit as e:
        assert e.code == 3
    assert c.writes() == []


def test_gate_refuses_same_account_and_staff_and_api_key_and_short_token():
    for claims, kind in ((good_claims(account=SRC_ACCT), "jwt"),
                         (good_claims(feature_flags=["staff"]), "jwt"),
                         (good_claims(), "api_key"),
                         (good_claims(exp=int(time.time()) + 600), "jwt"),
                         (good_claims(impersonated=True), "jwt")):
        c = Stub(target_script(), kind=kind, claims=claims)
        try:
            run(min_blueprint(), c)
            assert False, (claims, kind)
        except SystemExit as e:
            assert e.code == 3, (claims, kind)
        assert c.writes() == []


def test_gate_live_read_must_match_claims():
    s = target_script()
    s[("GET", "/api/internal/sequence/?limit=5")] = rows({"uuid": "q", "account_uuid": SRC_ACCT})
    c = Stub(s, claims=good_claims())
    try:
        run(min_blueprint(), c)
        assert False
    except SystemExit as e:
        assert e.code == 3


def test_gate_allows_same_account_only_with_reason():
    c = Stub(target_script(), claims=good_claims(account=SRC_ACCT, feature_flags=["staff"]))
    code = run(min_blueprint(), c, allow_same_account="restore test", allow_staff_target=True)
    assert code == 0 and c.writes() == [], "dry run makes no writes"


def test_empty_user_index_refuses():
    s = target_script()
    s[("GET", "/api/internal/account/user/?offset=0&limit=999")] = []
    c = Stub(s, claims=good_claims())
    try:
        run(min_blueprint(), c)
        assert False
    except SystemExit as e:
        assert e.code == 3


# ---- dry run and plan

def test_dry_run_makes_no_writes_and_plans_everything():
    c = Stub(target_script(), claims=good_claims())
    code = run(min_blueprint(), c)
    assert code == 0
    assert c.writes() == [], c.writes()


def test_listing_429_aborts_not_empty():
    s = target_script()
    s[("GET", "/api/internal/tag/?offset=0&limit=10000&ordering=title")] = C.ApiError(429, "GET", "/tag", "available in 40 seconds")
    c = Stub(s, claims=good_claims())
    try:
        run(min_blueprint(), c, commit=True)
        assert False
    except C.ApiError as e:
        assert e.code == 429
    assert c.writes() == [], "a throttled listing must never read as 'nothing exists'"


# ---- commit paths, family by family

def commit_script(created):
    """Target that answers every create with a read-back that finds the object."""
    s = target_script()
    lists, tags, statuses, folders, presets, seqs, seqf, groups, tps, maps = ([] for _ in range(10))

    def mk(kind, body):
        created.append((kind, body))
        return {"uuid": "%s-%d" % (kind, len(created))}
    s[("POST", "/api/internal/status/")] = lambda b: (statuses.append(dict(b, uuid="st-x", created_by={"x": 1})) or mk("status", b))
    s[("GET", "/api/internal/status/?limit=1000")] = lambda b: rows(
        {"uuid": "ts1", "title": "new_lead", "color": "#7431d7", "is_active": True, "created_by": None, "order": 1},
        {"uuid": "ts2", "title": "cold lead", "color": "#000", "is_active": True, "created_by": {"x": 1}, "order": 3}, *statuses)
    s[("POST", "/api/internal/list/")] = lambda b: (lists.append({"uuid": "l-%d" % len(lists), **b}) or mk("list", b))
    s[("GET", "/api/internal/list/?limit=999")] = lambda b: rows(*lists)
    s[("POST", "/api/internal/tag/")] = lambda b: (tags.append({"uuid": "t-%d" % len(tags), **b}) or mk("tag", b))
    s[("GET", "/api/internal/tag/?offset=0&limit=10000&ordering=title")] = lambda b: rows(*tags)
    s[("POST", "/api/internal/task-group/")] = lambda b: (groups.append({"uuid": "g-1", **b}) or mk("task_group", b))
    s[("GET", "/api/internal/task-group/?offset=0&limit=999")] = lambda b: rows(*groups)
    s[("POST", "/api/internal/task-group/g-1/task-preset/")] = lambda b: (tps.append({"uuid": "tp-%d" % len(tps), **b}) or mk("task_preset", b))
    s[("GET", "/api/internal/task-group/g-1/task-preset/?offset=0&limit=999")] = lambda b: rows(*tps)
    s[("POST", "/api/internal/filter-preset-folder/")] = lambda b: (folders.append({"uuid": "fo-1", **b}) or mk("folder", b))
    s[("GET", "/api/internal/filter-preset-folder/?type=properties&limit=999")] = lambda b: rows(*folders)
    s[("GET", "/api/internal/filter-preset-folder/fo-1/filter-preset/?limit=999")] = lambda b: rows(*presets)
    s[("POST", "/api/internal/filter-preset/")] = lambda b: (presets.append({"uuid": "pr-1", **b}) or mk("preset", b))
    s[("GET", "/api/internal/filter-preset/pr-1/")] = lambda b: dict(presets[0])
    s[("POST", "/api/internal/sequence-folder/")] = lambda b: (seqf.append({"uuid": "sf-1", **b}) or mk("seq_folder", b))
    s[("GET", "/api/internal/sequence-folder/?limit=999")] = lambda b: rows(*seqf)
    s[("POST", "/api/internal/sequence/")] = lambda b: (seqs.append({"uuid": "sq-%d" % len(seqs), **b}) or mk("sequence", b))
    s[("GET", "/api/internal/sequence/?limit=999")] = lambda b: rows(*seqs)
    for i in range(6):
        s[("GET", "/api/internal/sequence/sq-%d/" % i)] = lambda b, i=i: dict(seqs[i], created="x", runs=0)
    s[("POST", "/filters/")] = lambda b: (maps.append({"id": 77, "is_active": True, **b}) or mk("siftmap", b))
    s[("GET", "/filters/?scope=account&page_size=100&page=1")] = lambda b: rows(*maps)
    s[("GET", "/filters/77/")] = lambda b: dict(maps[0])
    return s


def test_commit_creates_everything_in_order_with_readbacks():
    created = []
    c = Stub(commit_script(created), claims=good_claims())
    code = run(min_blueprint(), c, commit=True, user_map={"Tinaa": "Jane"})
    assert code == 0, code
    kinds = [k for k, _ in created]
    assert kinds == ["status", "list", "tag", "task_group", "task_preset", "folder", "preset", "seq_folder",
                     "sequence", "siftmap"], kinds
    st = dict(created[0][1])
    assert st == {"title": "Hot Lead", "color": "#ff0000", "is_active": True}, "status POST carries color; system statuses never posted"
    tp = dict(created[4][1])
    assert tp["assigned_to_user"] == "user-self-uuid", "unmapped assignee -> the applying user (placeholder)"
    assert tp["expires_in"] == {"times": "1", "period": "day"} and tp["round_robin"] is True
    pr = dict(created[6][1])
    must = pr["filters"]["must"]
    assert pr["filters"]["account"] == TGT_ACCT, "filters.account rewritten to the TARGET"
    assert must["any_tags"] == ["t-0"] and must["must_not"]["any_lists"] == ["l-0"], "refs resolved to target uuids"
    assert "any_neighborhood" not in must["must_not"], "market strings stripped by default"
    assert must["assigned_to"] == "user-jane", "--user-map Tinaa=Jane resolved"
    assert must["must_not"]["any_property_status"] == ["cold lead"], "status matched by case to the target's title"
    assert pr["quick_filter"] is True and pr["folder"] == "fo-1"
    sq = dict(created[8][1])
    assert sq["conditions"][0]["payload"]["values"] == ["t-0"] and sq["folder"] == "sf-1" and sq["is_active"] is True
    mp = dict(created[9][1])
    assert mp["auto_add_enabled"] is False, "SiftMap auto-add forced OFF by default"
    assert mp["tags"] == ["Priority 1"]
    # the default folder was not created
    assert all(b.get("title") != "default" for k, b in created if k == "folder")
    # count probe ran through the override header and nothing else touched /property/
    prop = [x for x in c.calls if x[1] == "/api/internal/property/"]
    assert prop and all(h.get("x-http-method-override") == "GET" for _, _, _, h in prop)


def test_preset_readback_mismatch_exits_2():
    created = []
    s = commit_script(created)
    s[("GET", "/api/internal/filter-preset/pr-1/")] = lambda b: {"uuid": "pr-1", "title": "Hottest - 02 Ready to Call",
                                                                 "quick_filter": True, "filters": {"must": {"phone": 1}}}
    c = Stub(s, claims=good_claims())
    code = run(min_blueprint(), c, commit=True, only={"lists", "tags", "presets"})
    assert code == 2, "a trimmed must on read-back is a mismatch, exit 2"


def test_only_leaves_other_families_as_resolve_only_gaps():
    created = []
    c = Stub(commit_script(created), claims=good_claims())
    code = run(min_blueprint(), c, commit=True, only={"presets"})
    assert code == 0
    assert [k for k, _ in created] == ["folder"], "a missing tag is a gap, not a create, when tags are not selected"


def test_dry_run_registers_containers_so_dependents_are_not_false_gaps():
    """Task groups, sequence folders and custom-field groups created in the dry
    pass must count for the objects below them, or a plan reports 13 task
    presets and 20 sequences as gaps the real run would create."""
    c = Stub(target_script(), claims=good_claims())
    code = run(min_blueprint(), c)
    assert code == 0 and c.writes() == []
    log = A.Log  # noqa: F841 (type only)
    # re-run capturing the log through run_apply's report path
    import tempfile
    d = tempfile.mkdtemp()
    rp = os.path.join(d, "plan")
    A.run_apply(Stub(target_script(), claims=good_claims()), min_blueprint(), "target@x.com", opts(report_path=rp))
    r = json.load(open(rp + ".json", encoding="utf-8"))
    gaps = [(e["family"], e["object"]) for e in r["log"] if e["action"] == "gap"]
    assert ("task_presets", "Call New Lead") not in gaps, gaps
    assert ("sequences", "Sold Tag") not in gaps, gaps
    fam = {s["family"]: s for s in r["summary"]}
    assert fam["task_presets"]["created"] == 1 and fam["sequences"]["created"] == 1


def test_move_presets_refolders_existing_with_readback():
    created = []
    s = commit_script(created)
    # the preset already exists, in a folder with the OLD name
    s[("GET", "/api/internal/filter-preset-folder/?type=properties&limit=999")] = lambda b: rows(
        {"uuid": "old-f", "title": "05. TIER 1 - FTM - CALL"},
        *[f for f in [] ])
    s[("GET", "/api/internal/filter-preset-folder/old-f/filter-preset/?limit=999")] = rows(
        {"uuid": "pr-9", "title": "Hottest - 02 Ready to Call"})
    patched = {}

    def patch(b):
        patched.update(b)
        return {}
    s[("PATCH", "/api/internal/filter-preset/pr-9/")] = patch
    s[("GET", "/api/internal/filter-preset/pr-9/")] = lambda b: {"uuid": "pr-9", "folder": patched.get("folder")}
    # folder create for the NEW name must still happen; its read-back lists both folders
    folders = [{"uuid": "old-f", "title": "05. TIER 1 - FTM - CALL"}]
    s[("POST", "/api/internal/filter-preset-folder/")] = lambda b: (folders.append({"uuid": "new-f", **b}) or {"uuid": "new-f"})
    s[("GET", "/api/internal/filter-preset-folder/?type=properties&limit=999")] = lambda b: rows(*folders)
    s[("GET", "/api/internal/filter-preset-folder/new-f/filter-preset/?limit=999")] = rows()
    c = Stub(s, claims=good_claims())
    code = run(min_blueprint(), c, commit=True, only={"presets"}, move_presets=True)
    assert code == 0
    assert patched == {"folder": "new-f"}, "only the folder is PATCHed, never the filters"
    # without the flag it is a gap and nothing is PATCHed
    patched.clear()
    folders[:] = [{"uuid": "old-f", "title": "05. TIER 1 - FTM - CALL"}]
    c2 = Stub(s, claims=good_claims())
    run(min_blueprint(), c2, commit=True, only={"presets"})
    assert not patched and not [x for x in c2.calls if x[0] == "PATCH"]


def test_boards_and_columns_created_through_probe_and_property_assign_falls_back():
    created = []
    s = commit_script(created)
    bp = min_blueprint()
    bp["boards"].append({"title": "Deep Prospecting", "columns": ["Research", "Signer Found"]})
    bp["boards"][0]["columns"].append("Send to Acquisitions")
    bp["sequences"].append({
        "title": "Call New Lead", "folder": "Lead Management", "is_active": True,
        "trigger": "property.status.updated",
        "conditions": [{"condition": "from_to", "payload": {"field": "status", "updated_to": "new_lead"}}],
        "actions": [{"action": "property-assign", "payload": {"field": "assigned_to", "value": B.ref("user", "Rami")}},
                    {"action": "create-siftline-card", "payload": {"values": {
                        "board": B.ref("board", "Deep Prospecting"),
                        "column": B.ref("column", "Research", board="Deep Prospecting")}}},
                    {"action": "create-task-by-preset", "payload": {
                        "task_preset": B.ref("task_preset", "Call New Lead", group="Lead Management")}}]})
    boards = [{"uuid": "tb1", "title": "Lead Management"}]
    cols = {"tb1": [{"uuid": "tc1", "title": "New Lead (Unqualified)"}], "tb2": []}
    s[("GET", "/api/internal/siftline/board/?offset=0&limit=999")] = lambda b: rows(*boards)
    s[("POST", "/api/internal/siftline/board/")] = lambda b: (boards.append({"uuid": "tb2", **b}) or created.append(("board", b)) or {"uuid": "tb2"})
    for buid in ("tb1", "tb2"):
        s[("GET", "/api/internal/siftline/board/%s/column/?offset=0&limit=999" % buid)] = (
            lambda b, buid=buid: rows(*cols[buid]))
        s[("POST", "/api/internal/siftline/board/%s/column/" % buid)] = (
            lambda b, buid=buid: (cols[buid].append({"uuid": "c-%s-%s" % (buid, b["title"]), **b})
                                   or created.append(("column", b)) or {}))
    c = Stub(s, claims=good_claims())
    code = run(bp, c, commit=True, only={"tags", "boards", "task_presets", "sequences"})
    assert code == 0
    kinds = [k for k, _ in created]
    assert kinds.count("board") == 1 and kinds.count("column") == 3, kinds
    seqs = [b for k, b in created if k == "sequence"]
    call = next(q for q in seqs if q["title"] == "Call New Lead")
    assert call["actions"][0]["payload"]["value"] == "user-self-uuid", "property-assign falls back to the applying user"
    assert call["actions"][1]["payload"]["values"] == {"board": "tb2", "column": "c-tb2-Research"}
    task = call["actions"][2]["payload"]
    assert task["task_preset"] == "tp-0" and task["timezone"] == "America/New_York"
    import re as _re
    assert _re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.999Z$", task["end_of_day"]), \
        "create-task-by-preset REQUIRES end_of_day on create; computed fresh, never copied stale"
    # dry run registers the created board so the plan for the sequence is honest
    c2 = Stub(s, claims=good_claims())
    import tempfile
    rp = os.path.join(tempfile.mkdtemp(), "plan")
    boards[:] = [{"uuid": "tb1", "title": "Lead Management"}]
    cols["tb1"] = [{"uuid": "tc1", "title": "New Lead (Unqualified)"}]
    cols["tb2"] = []
    A.run_apply(c2, bp, "target@x.com", opts(report_path=rp, only={"tags", "boards", "task_presets", "sequences"}))
    r = json.load(open(rp + ".json", encoding="utf-8"))
    assert not [e for e in r["log"] if e["action"] == "gap" and e["family"] == "sequences" and e["object"] == "Call New Lead"]
    assert c2.writes() == []


def test_probe_405_marks_family_manual_and_continues():
    created = []
    s = commit_script(created)
    s[("POST", "/api/internal/task-group/")] = C.ApiError(405, "POST", "/api/internal/task-group/", "")
    c = Stub(s, claims=good_claims())
    code = run(min_blueprint(), c, commit=True)
    assert code == 0, "an unavailable route is a reported gap, not a failure"
    kinds = [k for k, _ in created]
    assert "task_group" not in kinds and "task_preset" not in kinds and "list" in kinds and "preset" in kinds


def test_probe_only_creates_one_per_route_and_stops():
    created = []
    c = Stub(commit_script(created), claims=good_claims())
    code = run(min_blueprint(), c, commit=True, probe_only=True)
    assert code == 0
    kinds = [k for k, _ in created]
    assert kinds.count("task_group") == 1 and kinds.count("seq_folder") == 1
    assert "siftmap" not in kinds, "--probe-only stops before SiftMap"


def test_sequence_with_unresolved_trigger_column_is_skipped():
    created = []
    bp = min_blueprint()
    bp["sequences"][0]["conditions"] = [{"condition": "from_to", "payload": {
        "field": "column", "meta": {"boards": [B.ref("board", "Lead Management")]},
        "updated_to": B.ref("column", "Gone Column", board="Lead Management")}}]
    c = Stub(commit_script(created), claims=good_claims())
    code = run(bp, c, commit=True, only={"sequences"})
    assert code == 0 and "sequence" not in [k for k, _ in created], "dangling trigger column -> never created"


def test_sequence_manual_action_creates_inactive_with_todo():
    created = []
    bp = min_blueprint()
    bp["sequences"][0]["actions"].append({"action": "send-sms", "$manual": True, "kept": {"message": "hi"}})
    bp["sequences"][0]["manual"] = ["send-sms removed"]
    c = Stub(commit_script(created), claims=good_claims())
    code = run(bp, c, commit=True, only={"tags", "sequences"})
    assert code == 0
    sq = [b for k, b in created if k == "sequence"][0]
    assert sq["is_active"] is False and len(sq["actions"]) == 1, "manual action dropped, sequence inactive"


def test_network_error_after_post_relists_before_counting():
    created = []
    s = commit_script(created)
    holder = {}

    def post_list(b):
        created.append(("list", b))
        # server committed, then the connection dropped
        holder["c"].script[("GET", "/api/internal/list/?limit=999")] = lambda bb: rows({"uuid": "l-0", "title": b["title"]})
        raise C.NetworkError("reset")
    s[("POST", "/api/internal/list/")] = post_list
    c = Stub(s, claims=good_claims())
    holder["c"] = c
    code = run(min_blueprint(), c, commit=True, only={"lists"})
    assert code == 0
    assert [k for k, _ in created].count("list") == 1, "no blind retry after a network error"


def test_list_title_whitespace_and_case_adopt_target_form():
    """ty+2 holds "Arrests " (trailing space); the server refuses a create that
    differs from an existing list only by case or whitespace. Export strips,
    apply adopts the target's form, and a 400 unique still resolves."""
    created = []
    s = commit_script(created)
    s[("GET", "/api/internal/list/?limit=999")] = lambda b: rows({"uuid": "l-arr", "title": "arrests"},
                                                                {"uuid": "l-auc", "title": "Auction"})
    bp = min_blueprint()
    bp["lists"].append({"title": "Arrests"})
    c = Stub(s, claims=good_claims())
    code = run(bp, c, commit=True, only={"lists"})
    assert code == 0 and not [k for k, _ in created if k == "list"], "no create for a case/space variant"
    # server-side unique rule the listing did not reveal: adopt after the 400
    s2 = commit_script(created)
    s2[("POST", "/api/internal/list/")] = C.ApiError(400, "POST", "/api/internal/list/",
                                                      '{"non_field_errors":["The fields title must make a unique set."]}')
    s2[("GET", "/api/internal/list/?limit=999")] = lambda b: rows({"uuid": "l-auc", "title": "AUCTION "})
    c2 = Stub(s2, claims=good_claims())
    code = run(min_blueprint(), c2, commit=True, only={"lists", "tags", "presets"})
    assert code == 0
    pr = [b for k, b in created if k == "preset"][-1]
    assert pr["filters"]["must"]["must_not"]["any_lists"] == ["l-auc"], "preset resolves to the adopted list"
    # export strips
    ec = fake_export_client()
    ec.script[("GET", "/api/internal/list/?limit=999")] = rows({"uuid": U["list_auction"], "title": "Auction  "})
    ebp = E.Exporter(ec, with_counts=False).run()
    assert [x["title"] for x in ebp["lists"]] == ["Auction"]


def test_task_preset_assigned_to_users_never_empty_and_failed_route_not_sticky():
    created = []
    s = commit_script(created)
    bp = min_blueprint()
    bp["task_presets"].append({"group": "Lead Management", "title": "Make Offer", "notes": None,
                               "round_robin": True, "expires_in": {"times": "0", "period": "day"},
                               "all_day": False, "due_time": "20:00:00", "assigned_to_user": None,
                               "assigned_to_users": [], "assigned_to_role": "sensei", "order": 1,
                               "skip_weekends": False})
    c = Stub(s, claims=good_claims())
    assert run(bp, c, commit=True, only={"task_presets"}) == 0
    tps = [b for k, b in created if k == "task_preset"]
    assert len(tps) == 2
    keys = ("assigned_to_user", "assigned_to_users", "assigned_to_role")
    for b in tps:
        assert sum(1 for k in keys if k in b) == 1, "exactly ONE assignee key, the other two absent: %s" % b
    assert tps[0]["assigned_to_user"] == "user-self-uuid", "Adriana unmapped -> the applying user"
    assert tps[1] == {**tps[1], "assigned_to_role": "sensei"} and "assigned_to_users" not in tps[1]
    # a state file remembering a payload rejection must not block the retry
    import tempfile
    sp = os.path.join(tempfile.mkdtemp(), "state.json")
    with open(sp, "w") as f:
        json.dump({"target": {"account": TGT_ACCT}, "routes": {"task_preset": "failed", "task_group": "verified"},
                   "families": {}}, f)
    created.clear()
    c2 = Stub(commit_script(created), claims=good_claims())
    A.run_apply(c2, min_blueprint(), "target@x.com", opts(commit=True, only={"task_presets"}, state_path=sp))
    assert [k for k, _ in created] == ["task_group", "task_preset"]


def test_state_refuses_another_account(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    sp = os.path.join(d, "state.json")
    with open(sp, "w") as f:
        json.dump({"target": {"account": "someone-else"}, "routes": {}, "families": {}}, f)
    c = Stub(target_script(), claims=good_claims())
    try:
        A.run_apply(c, min_blueprint(), "target@x.com", opts(state_path=sp))
        assert False
    except SystemExit as e:
        assert e.code == 3


def test_verify_phase_reports_missing_as_gaps_without_writes():
    c = Stub(target_script(), claims=good_claims())
    code = run(min_blueprint(), c, verify_only=True)
    assert code == 0 and c.writes() == []


# ---- pure helpers

def test_client_retry_hint_and_get_all_shapes():
    assert C._RETRY_HINT.search("Request was throttled. Expected available in 42 seconds.").group(1) == "42"
    assert C.decode_claims(jwt(email="a@b.c"))["email"] == "a@b.c"
    st = Stub({("GET", "/x"): [{"uuid": "1"}]})
    assert st.get_all("/x") == [{"uuid": "1"}], "flat array listing"


def test_count_shape_flattens_and_drops_relative_dates():
    m, approx = E.count_shape({"phone": 1, "ownerPropertiesOwned": {"show_properties": "one_per_owner"},
                               "last_direct_mailed": ["36-months", "month"], "_ui": {}})
    assert m == {"phone": 1, "show_properties": "one_per_owner"} and approx is True


def test_subset_equal():
    assert T.subset_equal({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2, "c": 3}], "uuid": "x"})
    assert not T.subset_equal({"a": [1, 2]}, {"a": [1]})
    assert not T.subset_equal({"a": 1}, {"b": 1})


def test_community_modules_are_stdlib_only():
    import importlib
    import sys as _sys
    before = set(_sys.modules)
    for m in ("client", "blueprint", "translate", "apply", "probe", "report", "cli"):
        importlib.import_module("account_blueprint." + m)
    new = {m for m in set(_sys.modules) - before if not m.startswith("account_blueprint")}
    bad = {m for m in new if m.split(".")[0] in ("reisift_auth", "crm_api", "requests", "siftmap_pull")}
    assert not bad, bad


def test_skill_copy_matches_source_package():
    """skills/account-blueprint ships a COPY of the package; it must not drift."""
    root = os.path.join(os.path.dirname(__file__), "..")
    src = os.path.join(root, "src", "account_blueprint")
    dst = os.path.join(root, "skills", "account-blueprint", "scripts", "account_blueprint")
    for name in sorted(os.listdir(src)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(src, name), "rb") as a, open(os.path.join(dst, name), "rb") as b:
            assert a.read() == b.read(), "%s drifted: run python tools/sync_account_blueprint_skill.py" % name
    with open(os.path.join(root, "src", "clone_account.py"), "rb") as a,             open(os.path.join(root, "skills", "account-blueprint", "scripts", "clone_account.py"), "rb") as b:
        assert a.read() == b.read()


def test_families_order_is_dependency_order():
    assert FAMILIES.index("tags") < FAMILIES.index("presets") < FAMILIES.index("sequences") < FAMILIES.index("siftmap")
    assert FAMILIES.index("task_presets") < FAMILIES.index("sequences")


if __name__ == "__main__":
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
