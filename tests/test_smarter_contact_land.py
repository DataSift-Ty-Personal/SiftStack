"""Zero-network checks for the land lane in Smarter Contact: the parcel patch, reply routing, templates, launch.

    python tests/test_smarter_contact_land.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import smarter_contact_land as L  # noqa: E402
import smarter_contact_templates as T  # noqa: E402
from sms_agent import sc_client  # noqa: E402

N = 0


def check(cond, msg):
    global N
    N += 1
    if not cond:
        raise AssertionError(msg)


U1 = "5d47cfc2-81b5-45d0-ae05-21e3aecc5a63"
U2 = "11111111-2222-4333-8444-555555555555"

# parcel numbers as an owner reads them on a tax bill
check(L.parcel_display("019kb024") == "019KB024", "Knox map/group/parcel upper-cased")
check(L.parcel_display("024  00501") == "024 00501", "double space collapsed")
check(L.parcel_display("019-028.00") == "019-028.00", "dashed counties keep their dashes")
check(L.acres_display(42.37) == "42" and L.acres_display(6.43) == "6.4" and L.acres_display(0.62) == "0.62",
      "acres read naturally per size")
check(L.acres_display(5.0) == "5" and L.acres_display(None) == "" and L.acres_display(0) == "", "acres edge cases")

# record uuid -> dataflik id, from pull_by_ids state files
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "pilot_lots"
    p.mkdir(parents=True)
    (p / "state.json").write_text(json.dumps({"new_uuids": {"123": U1}, "existing": {"456": U2},
                                              "sent": ["123"]}), encoding="utf-8")
    (Path(d) / "junk.json").write_text("[1, 2]", encoding="utf-8")
    other = Path(d) / "data_staging" / "franklin"
    other.mkdir(parents=True)
    (other / "state.json").write_text(json.dumps({"new_uuids": {"999": U2.replace("1", "7")}}), encoding="utf-8")
    um = L.uuid_map(Path(d))
check(um == {U1: "123", U2: "456"}, "both new and existing records map back; another account's pull is never read")

# which field holds the record, and what gets written
check(L.record_uuid({"SIFT_RECORD": U2, "ASSESSOR_PARCEL_NUMBER": U1}) == U2, "Sift Record wins over APN")
check(L.record_uuid({"ASSESSOR_PARCEL_NUMBER": U1}) == U1, "an unpatched push still resolves through APN")
check(L.record_uuid({"ASSESSOR_PARCEL_NUMBER": "019KB024"}) == "", "a parcel number is not a record id")
row = {"dataflik_id": 123, "fips": "47093", "parcel_number": "019kb024", "acres": 0.18,
       "property_address": "Rutledge Pike"}
ch = L.wanted({"ASSESSOR_PARCEL_NUMBER": U1, "ADDRESS": "Rutledge Pike"}, row, U1)
check(ch == {"ASSESSOR_PARCEL_NUMBER": "019KB024", "PARCEL_ACRES": "0.18", "PARCEL_COUNTY": "Knox",
             "PARCEL_STATE": "TN", "SIFT_RECORD": U1}, "the four parcel fields plus the record id")
check("ADDRESS" not in ch, "a present address is never overwritten")
ch2 = L.wanted({"ASSESSOR_PARCEL_NUMBER": U1, "ADDRESS": ""}, row, U1)
check(ch2.get("ADDRESS") == "Rutledge Pike", "a blank address gets the road")
done = {"ASSESSOR_PARCEL_NUMBER": "019KB024", "PARCEL_ACRES": "0.18", "PARCEL_COUNTY": "Knox",
        "PARCEL_STATE": "TN", "SIFT_RECORD": U1, "ADDRESS": "Rutledge Pike"}
check(L.wanted(done, row, U1) == {}, "an already patched contact is skipped")

# the write carries every field, so a replace-style update cannot drop the phone
attrs = [{"fieldId": "f-phone", "value": "8655550100"}, {"fieldId": "f-apn", "value": U1},
         {"fieldId": "f-first", "value": "Agnes"}]
keys = {"ASSESSOR_PARCEL_NUMBER": "f-apn", "SIFT_RECORD": "f-sift", "PARCEL_COUNTY": "f-county"}
out = {a["fieldId"]: a["value"] for a in L.full_attrs(attrs, {"ASSESSOR_PARCEL_NUMBER": "019KB024",
                                                              "SIFT_RECORD": U1, "PARCEL_COUNTY": "Knox"}, keys)}
check(out == {"f-phone": "8655550100", "f-apn": "019KB024", "f-first": "Agnes", "f-sift": U1, "f-county": "Knox"},
      "full attribute list with the changes applied")
check(L.values_by_key(attrs, {"f-apn": "ASSESSOR_PARCEL_NUMBER", "f-phone": "PHONE"}) ==
      {"ASSESSOR_PARCEL_NUMBER": U1, "PHONE": "8655550100"}, "values keyed by field key")

# reply routing reads Sift Record first, then APN, and never returns a parcel number
calls = []


def fake_gql(q, v=None):
    calls.append(q)
    if "findFields" in q:
        return {"findFields": [{"id": "f-apn", "key": "ASSESSOR_PARCEL_NUMBER", "name": "APN"},
                               {"id": "f-sift", "key": None, "name": "Sift Record"}]}
    return {"getContact": {"attrs": CONTACTS[v["id"]]}}


CONTACTS = {"land": [{"fieldId": "f-apn", "value": "019KB024"}, {"fieldId": "f-sift", "value": U2}],
            "p1": [{"fieldId": "f-apn", "value": U1}],
            "bare": [{"fieldId": "f-apn", "value": "019KB024"}]}
real = sc_client.gql
sc_client.gql = fake_gql
sc_client._record_fields.update(apn="", sift="", read=False)
try:
    check(sc_client.contact_record_uuid("land") == U2, "a patched land contact routes through Sift Record")
    check(sc_client.contact_record_uuid("p1") == U1, "a Priority 1 contact still routes through APN")
    check(sc_client.contact_record_uuid("bare") == "", "a parcel number alone routes nowhere (phone map fallback)")
    check(sum("findFields" in q for q in calls) == 1, "the field ids are read once")
finally:
    sc_client.gql = real
    sc_client._record_fields.update(apn="", sift="", read=False)

# templates: the land set passes every gate and maps to Smarter Contact's parcel merge tags
check(T.land_audit() == [], "every land variant passes the land profile: %s" % T.land_audit()[:3])
check(T.audit() == [], "Priority 1 templates untouched and still passing")
s = T.to_sc("Hi {first}, the {acres} acres at {addr} in {county} County, parcel {apn}? {sender}", "Adriana")
check(s == "Hi <First Name>, the <Parcel acres> acres at <Address> in <Parcel county> County, parcel <APN>? Adriana",
      "land placeholders become Smarter Contact merge tags")
check(all("{apn}" in t or "{addr}" in t for _, n, nm, nn in T.land_arms() for t in nm + nn),
      "every land text names the parcel or its address")
check(all("{apn}" in t for lane, n, nm, nn in T.land_arms() if n == 1 and lane == "lot" for t in nm + nn),
      "every lot opener names the parcel number")
check(not any("vacant" in t.lower() for lane, _, nm, nn in T.land_arms() if lane == "tract" for t in nm + nn),
      "tracts never say vacant")
check(T.problems("Hi {first}, is the vacant lot at {addr} still yours? {sender}", kind="touch", key="1",
                 profile="land") == [], "vacant is allowed on the land profile")
check(T.problems("Hi {first}, is the vacant lot at {addr} still yours? {sender}", kind="touch", key="1") != [],
      "and still refused on Priority 1")
titles = [T.land_title(lane, n, aud, i) for lane, n, nm, nn in T.land_arms()
          for aud, pool in (("named", nm), ("noname", nn)) for i in range(1, len(pool) + 1)]
check(len(titles) == len(set(titles)) and max(map(len, titles)) <= T.SC_TEMPLATE_TITLE,
      "land template titles unique and under Smarter Contact's 35 characters")
check(not set(titles) & {T.title(n, a.title(), aud, i) for n, a, nm, nn in T._arms()
                         for aud, pool in (("named", nm), ("noname", nn)) for i in range(1, len(pool) + 1)},
      "land titles never collide with Priority 1 titles")

# launch: --land swaps the template titles, Priority 1 stays as it was
import smarter_contact_launch as SL  # noqa: E402

SL.LAND["lane"] = ""
check(SL._titles(1, "named")[0] == T.title(1, "", "named", 1), "Priority 1 titles unchanged without --land")
SL.LAND["lane"] = "tract"
check(SL._titles(1, "named") == [T.land_title("tract", 1, "named", i) for i in range(1, 9)], "tract touch 1 titles")
check(len(SL._titles(2, "noname")) == 4, "tract touch 2 no-name titles")
SL.LAND["lane"] = ""

# no stray control bytes (a shell heredoc once turned a word boundary into a backspace)
for mod in (L, T, SL, sc_client):
    with open(mod.__file__, "rb") as fh:
        check(b"\x08" not in fh.read(), "no backspace byte in %s" % os.path.basename(mod.__file__))

print("test_smarter_contact_land: %d checks passed" % N)
