"""datasift_api_upload: the create route and the payload contract.

Zero network. A stub stands in for Api and records every call, so the tests
assert the ROUTE and the BODY that would go over the wire.

Why this exists: on 2026-09-01 DataSift's Open API route POST /property/
began returning 403 for every method under both auth types, and five daily
FTM runs scraped cleanly and uploaded nothing. create_property() now writes
through /api/internal/property/, which does NOT upsert; a duplicate address
comes back as a 400 carrying the existing uuid, and lists/tags are attached
to that record instead.

Run: python tests/test_datasift_api_upload.py   (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import datasift_api_upload as U  # noqa: E402

DUP = ('{"non_field_errors":["Property address already exists!"],'
       '"property":["11111111-2222-3333-4444-555555555555"]}')


class Stub:
    """Scripted Api: `script` maps (method, path) -> response or ApiError."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path, body, headers))
        r = self.script.get((method, path))
        if isinstance(r, Exception):
            raise r
        return r if r is not None else {}


def err(code, path, body=""):
    return U.ApiError(code, "POST", path, body)


def test_internal_route_first():
    api = Stub({("POST", U.CREATE_ROUTE): {"uuid": "new-1"}})
    U._ROUTE_DECIDED["route"] = ""
    res = U.create_property(api, {"address": {"street": "1 A St"}, "owner": {"first_name": "A"}})
    assert res["uuid"] == "new-1" and not res.get("existing")
    assert [c[1] for c in api.calls] == [U.CREATE_ROUTE]


def test_legacy_fallback_only_on_403_404():
    for code in (403, 404):
        api = Stub({("POST", U.CREATE_ROUTE): err(code, U.CREATE_ROUTE),
                    ("POST", U.LEGACY_CREATE_ROUTE): {"uuid": "legacy-1"}})
        U._ROUTE_DECIDED["route"] = ""
        res = U.create_property(api, {"address": {"street": "1 A St"}, "owner": {"first_name": "A"}})
        assert res["uuid"] == "legacy-1"
        assert [c[1] for c in api.calls] == [U.CREATE_ROUTE, U.LEGACY_CREATE_ROUTE]


def test_other_errors_propagate():
    api = Stub({("POST", U.CREATE_ROUTE): err(500, U.CREATE_ROUTE, "boom")})
    try:
        U.create_property(api, {"address": {"street": "1 A St"}, "owner": {"first_name": "A"}})
    except U.ApiError as e:
        assert e.code == 500 and "HTTP 500 on POST" in str(e)
    else:
        raise AssertionError("500 must propagate")
    assert len(api.calls) == 1, "no legacy retry on a 500"


def test_duplicate_address_attaches_to_existing():
    uuid = "11111111-2222-3333-4444-555555555555"
    api = Stub({
        ("POST", U.CREATE_ROUTE): err(400, U.CREATE_ROUTE, DUP),
        ("GET", "/api/internal/property/%s/" % uuid): {
            "tags": ["Courthouse Data", "knox"],
            "owner": {"first_name": "Clyde", "last_name": "Lay", "company": None}},
    })
    body = {"address": {"street": "6520 Flint Gap Rd", "postal_code": "37914"},
            "owner": {"first_name": "Clyde", "last_name": "Lay", "address": {}},
            "lists": "Foreclosure", "tags": ["Courthouse Data", "FTM", "knox"]}
    res = U.create_property(api, body)
    assert res == {"uuid": uuid, "existing": True}
    paths = [(c[0], c[1]) for c in api.calls]
    assert ("POST", "/api/internal/property/%s/add-lists/" % uuid) in paths
    add = [c for c in api.calls if c[1].endswith("/add-lists/")]
    assert add[0][2] == {"lists": "Foreclosure"}, "add-lists takes a STRING title"
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch) == 1
    assert patch[0][2] == {"tags": ["Courthouse Data", "knox", "FTM"]}, "read-modify-write, no dupes"
    assert "owner" not in patch[0][2], "same owner means no owner write"


def test_owner_change_is_patched():
    uuid = "11111111-2222-3333-4444-555555555555"
    api = Stub({
        ("POST", U.CREATE_ROUTE): err(400, U.CREATE_ROUTE, DUP),
        ("GET", "/api/internal/property/%s/" % uuid): {
            "tags": [], "owner": {"first_name": "Wrong", "last_name": "Person"}},
    })
    body = {"address": {"street": "1 A St"}, "owner": {"first_name": "Right", "last_name": "Person"}}
    U.create_property(api, body)
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert patch and patch[0][2] == {"owner": body["owner"]}


def _attach(rec, body):
    uuid = "11111111-2222-3333-4444-555555555555"
    api = Stub({("POST", U.CREATE_ROUTE): err(400, U.CREATE_ROUTE, DUP),
                ("GET", "/api/internal/property/%s/" % uuid): rec})
    U.create_property(api, body)
    patches = [c[2] for c in api.calls if c[0] == "PATCH"]
    notes = [c[2]["notes"] for c in api.calls if c[1].endswith("/add-notes/")]
    return patches, notes


def test_a_researched_record_keeps_its_owner():
    # Research put a verified heir in the owner slot. A republished notice names the dead borrower again.
    rec = {"tags": ["FTM", "Deep Prospected"], "owner": {"first_name": "Dana", "last_name": "Heir"}}
    body = {"address": {"street": "1 A St"}, "owner": {"first_name": "Morgan", "last_name": "Borrower"}, "tags": ["FTM"]}
    patches, notes = _attach(rec, body)
    assert not any("owner" in p for p in patches), patches
    assert notes and "Morgan Borrower" in notes[0] and "kept" in notes[0], notes


def test_a_deceased_owner_is_not_replaced_by_a_notice():
    rec = {"tags": [], "owner": {"first_name": "Morgan", "last_name": "Borrower", "deceased": True}}
    body = {"address": {"street": "1 A St"}, "owner": {"first_name": "Riley", "last_name": "Guess"}}
    patches, notes = _attach(rec, body)
    assert not any("owner" in p for p in patches) and notes, (patches, notes)


def test_a_probate_notice_still_names_the_personal_representative():
    # The court named this person: the one case where an upload replaces a deceased owner, with a note.
    rec = {"tags": ["Heir Identified"], "owner": {"first_name": "Morgan", "last_name": "Borrower", "deceased": True}}
    body = {"address": {"street": "1 A St"}, "owner": {"first_name": "Casey", "last_name": "Executor"}, "lists": "Probate"}
    patches, notes = _attach(rec, body)
    assert any(p.get("owner") == body["owner"] for p in patches), patches
    assert notes and "personal representative" in notes[0] and "Morgan Borrower" in notes[0], notes


def test_an_ordinary_record_is_patched_without_a_note():
    rec = {"tags": ["FTM"], "owner": {"first_name": "Wrong", "last_name": "Person"}}
    body = {"address": {"street": "1 A St"}, "owner": {"first_name": "Right", "last_name": "Person"}}
    patches, notes = _attach(rec, body)
    assert any(p.get("owner") == body["owner"] for p in patches) and not notes, (patches, notes)


def test_first_upload_owner_slot_is_switchable():
    # Off by default: the enricher's decision maker goes in the owner slot, as it always has.
    # With the switch on, a deceased owner of record stays, except on a probate notice.
    import importlib
    import datasift_formatter as F
    from notice_parser import NoticeData
    n = NoticeData()
    n.owner_name, n.owner_deceased, n.decision_maker_name = "Morgan Borrower", "yes", "Riley Guess"
    n.address, n.city, n.state, n.zip, n.notice_type = "1 A St", "Knoxville", "TN", "37917", "foreclosure"
    os.environ.pop("UPLOAD_KEEP_OWNER_OF_RECORD", None)
    assert F._get_contact_info(n)["first"] == "Riley"
    os.environ["UPLOAD_KEEP_OWNER_OF_RECORD"] = "1"
    try:
        assert F._get_contact_info(n)["first"] == "Morgan", F._get_contact_info(n)
        n.notice_type = "probate"
        assert F._get_contact_info(n)["first"] == "Riley"
    finally:
        os.environ.pop("UPLOAD_KEEP_OWNER_OF_RECORD", None)
    assert importlib  # noqa


def test_address_only_body_is_a_lookup():
    uuid = "aaaa"
    api = Stub({
        ("POST", "/api/internal/property/"): {"results": [
            {"uuid": "bbbb", "address": {"street": "6520 Flint Gap Rd", "zip5": "37920"}},
            {"uuid": uuid, "address": {"street": "6520 Flint Gap Rd", "zip5": "37914"}}]},
        ("GET", "/api/internal/property/%s/" % uuid): {"tags": []},
    })
    res = U.create_property(api, {"address": {"street": "6520 FLINT GAP RD", "postal_code": "37914"},
                                  "tags": ["Dispo Traced"]})
    assert res == {"uuid": uuid, "existing": True}, "zip5 disambiguates, case ignored"
    search = api.calls[0]
    assert search[0] == "POST" and search[1] == "/api/internal/property/"
    assert search[3] == {"x-http-method-override": "GET"}, "POST-as-GET, never a bare create"
    assert search[2]["query"]["must"]["search"] == "address_prefix:6520 FLINT GAP RD"


def test_address_only_miss_raises():
    api = Stub({("POST", "/api/internal/property/"): {"results": []}})
    try:
        U.create_property(api, {"address": {"street": "1 Nowhere Ln"}})
    except RuntimeError as e:
        assert "no owner" in str(e)
    else:
        raise AssertionError("must not silently create without an owner")


def test_existing_uuid_parser_is_strict():
    assert U._existing_uuid(err(400, "x", DUP)) == "11111111-2222-3333-4444-555555555555"
    assert U._existing_uuid(err(400, "x", '{"owner":["This field is required."]}')) == ""
    assert U._existing_uuid(err(403, "x", DUP)) == ""
    assert U._existing_uuid(err(400, "x", "not json")) == ""


def test_build_property_contract():
    row = {"Property Street Address": "1 A St", "Property City": "Knoxville",
           "Property State": "TN", "Property ZIP Code": "37914",
           "Owner First Name": "", "Owner Last Name": "ACME HOLDINGS LLC",
           "Tags": "Courthouse Data, knox", "Lists": "Foreclosure"}
    b = U.build_property(row)
    assert b["tags"] == ["Courthouse Data", "knox"], "tags are an ARRAY"
    assert b["owner"] == {"address": b["owner"]["address"], "company": "ACME HOLDINGS LLC"}, \
        "entity owner: company, and NO person keys at all"
    row2 = dict(row, **{"Owner First Name": "Clyde", "Owner Last Name": "Lay"})
    assert U.build_property(row2)["owner"]["first_name"] == "Clyde"




IDX = {"Notice Type": {"uuid": "f-type", "field_type": "select", "options": {"foreclosure": "opt-fc"}},
       "County": {"uuid": "f-county", "field_type": "select", "options": {"Knox": "opt-knox"}},
       "Foreclosure Date": {"uuid": "f-sale", "field_type": "date", "options": {}}}


def test_sale_date_is_mapped_and_iso():
    """The 2026-11-11 find: the sale date was not in FIELD_MAP, so no foreclosure uploaded since
    2026-08-14 carried one. The formatter writes M/D/YYYY; DataSift stores YYYY-MM-DD."""
    warn = set()
    pairs = U.field_pairs({"Notice Type": "foreclosure", "County": "Knox", "Foreclosure Date": "11/24/2026"}, IDX, warn)
    assert {"field_uuid": "f-sale", "value": "2026-11-24"} in pairs, pairs
    assert not warn, warn


def test_unreadable_date_is_skipped_not_guessed():
    warn = set()
    pairs = U.field_pairs({"Notice Type": "foreclosure", "Foreclosure Date": "sometime in May"}, IDX, warn)
    assert [p["field_uuid"] for p in pairs] == ["f-type"], pairs
    assert any("Foreclosure Date" in w for w in warn), warn


def test_a_refused_batch_is_retried_field_by_field():
    path = "/api/internal/property/u1/custom-field/update-values/"

    class Picky:
        def __init__(self):
            self.calls = []

        def call(self, p, method="GET", body=None, headers=None):
            self.calls.append(body)
            if any(x["field_uuid"] == "f-sale" for x in body):
                raise U.ApiError(400, method, p, '{"value":["bad date"]}')
            return {}

    api, warn = Picky(), set()
    pairs = [{"field_uuid": "f-type", "value": "opt-fc"}, {"field_uuid": "f-county", "value": "opt-knox"},
             {"field_uuid": "f-sale", "value": "2026-11-24"}]
    landed = U.write_fields(api, "u1", pairs, warn)
    assert landed == 2, landed
    assert [len(b) for b in api.calls] == [3, 1, 1, 1], api.calls
    assert any("refused" in w for w in warn), warn
    assert path


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok ", fn.__name__)
    print("%d checks passed" % len(fns))
