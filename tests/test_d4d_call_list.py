"""Zero-network checks for src/d4d_call_list.py.

    python tests/test_d4d_call_list.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import d4d_call_list as d  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def ph(number, tier=None, status="UNKNOWN", type_="MOBILE", extra_tags=()):
    tags = list(extra_tags)
    if tier:
        tags.append(tier)
    return {"number": number, "type": type_, "tags": tags, "status": status, "is_connected": True}


# --- eligibility -------------------------------------------------------
check(d.is_eligible(ph("8655550100")), "untiered UNKNOWN mobile is eligible")
check(not d.is_eligible(ph("8655550100", tier="Dial First")), "a phone with a tier is not re-bought")
for st in ("WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"):
    check(not d.is_eligible(ph("8655550100", status=st)), "status %s is not eligible" % st)
check(d.is_eligible(ph("8655550100", status="NO_ANSWER")), "NO_ANSWER is still eligible")
check(not d.is_eligible(ph("55501", )), "a short number is not eligible")
check(not d.is_eligible(ph("8655550100", extra_tags=["DNC"])), "a litigator DNC tag is not eligible")
check(d.is_eligible(ph("+1 (865) 555-0100")), "formatted numbers clean to 10 digits")

recs = {"r1": {"owner_uuid": "o1", "phones": [ph("8655550100"), ph("8655550101", tier="Dial Third")]},
        "r2": {"owner_uuid": "o2", "phones": [ph("8655550100"), ph("8655550102", status="WRONG")]}}
elig = d.eligible_numbers(recs)
check(set(elig) == {"8655550100"}, "eligible numbers deduped across records: %s" % sorted(elig))
check(len(elig["8655550100"]) == 2, "a shared number keeps both holders")

# --- write-back payload ------------------------------------------------
p = ph("8655550100", tier="Dial Third", extra_tags=["dispo"], status="no_answer", type_="LANDLINE")
pay = d.payload_for_tier(p, "Dial First")
check(pay["tags"] == ["Dial First", "dispo"], "old tier stripped, new tier added, other tags kept: %s" % pay["tags"])
check(pay["status"] == "NO_ANSWER" and pay["type"] == "LANDLINE", "status upper-cased and type carried through")
check(pay["is_connected"] is True and pay["verified"] is False, "flags carried through")
pay2 = d.payload_for_tier({"number": "8655550100", "tags": [{"title": "Dial Fourth"}]}, "Dial Second")
check(pay2["tags"] == ["Dial Second"], "dict-shaped tags handled: %s" % pay2["tags"])

cache = {"8655550100": {"score": 92, "tier": "Dial First"},
         "8655550103": {"score": 10, "tier": "Drop"},
         "8655550104": {"score": 70, "tier": "DNC", "litigator": True}}
recs2 = {"r1": {"owner_uuid": "o1", "phones": [ph("8655550100"), ph("8655550101", tier="Dial Third"),
                                                 ph("8655550103"), ph("8655550104")]},
         "r3": {"owner_uuid": None, "phones": [ph("8655550100")]}}
plan = d.planned_writes(recs2, cache)
check(list(plan) == ["o1"], "one owner planned, ownerless record skipped: %s" % list(plan))
check(len(plan["o1"]["phones"]) == 3, "3 phones planned (tiered one skipped): %d" % len(plan["o1"]["phones"]))
check(plan["o1"]["expect"] == {"8655550100": "Dial First", "8655550103": "Drop", "8655550104": "DNC"},
      "expected tiers recorded per number")

# --- phone ordering ----------------------------------------------------
phones = [ph("8655550110", tier="Dial Fourth"), ph("8655550111", type_="LANDLINE", tier="Dial First"),
          ph("8655550112", tier="Dial First"), ph("8655550113"), ph("8655550114", tier="Drop"),
          ph("8655550115", status="WRONG", tier="Dial First"), ph("8655550116", tier="Dial Second"),
          ph("8655550117")]
cache2 = {"8655550113": {"score": 55, "tier": "Dial Third"}, "8655550117": {"score": 30, "tier": "Dial Fourth", "litigator": True}}
order = [q["phone"] for q in d.sort_phones(phones, cache2)]
check(order == ["8655550112", "8655550111", "8655550116", "8655550113", "8655550110", "8655550114"],
      "order: First mobile, First landline, Second, cached Third, Fourth, Drop; WRONG and litigator dropped: %s" % order)
check(d.sort_phones([ph("8655550118", status="CORRECT_DNC")], {}) == [], "CORRECT_DNC never exported")
check(d.fmt_phone("8655550100") == "(865) 555-0100", "display format")
check(d.fmt_phone("18655550100") == "(865) 555-0100", "11-digit cleans to display format")

# --- export rows + csv --------------------------------------------------
cohort = [{"rank": 1, "score": 100, "dataflik_id": "1", "address": "1 Main St, Knoxville, TN 37917", "zip": "37917",
           "neighborhood": "N", "county": "Knox", "est_value": 100000, "equity_pct": 50, "owner_label": "Absentee",
           "known_distress": "vacant", "saved_uuid": "u1"},
          {"rank": 2, "score": 99, "dataflik_id": "2", "address": "2 Main St, Knoxville, TN 37917", "zip": "37917",
           "neighborhood": "N", "county": "Knox", "est_value": 100000, "equity_pct": 50, "owner_label": "Owner-occ",
           "known_distress": "", "saved_uuid": "u2"},
          {"rank": 3, "score": 98, "dataflik_id": "3", "address": "3 Main St, Knoxville, TN 37917", "zip": "37917",
           "neighborhood": "N", "county": "Knox", "est_value": 100000, "equity_pct": 50, "owner_label": "Owner-occ",
           "known_distress": "", "saved_uuid": ""}]
recs3 = {"u1": {"owner_first": "Jane", "owner_last": "Doe", "owner_company": "", "mailing_street": "9 Elm St",
                "mailing_city": "Knoxville", "mailing_state": "TN", "mailing_zip": "37917", "street": "1 Main St",
                "city": "Knoxville", "state": "TN", "zip": "37917", "county": "Knox", "estimate_value": 120000,
                "equity_percent": 60, "status": "", "phones": phones, "pulled": False},
         "u2": {"owner_first": "", "owner_last": "", "owner_company": "Acme LLC", "mailing_street": "",
                "mailing_city": "", "mailing_state": "", "mailing_zip": "", "street": "2 Main St",
                "city": "Knoxville", "state": "TN", "zip": "37917", "county": "Knox", "estimate_value": None,
                "equity_percent": None, "status": "not_interested", "phones": [ph("8655550119", status="WRONG")],
                "pulled": True}}
call_rows, phone_rows, none_rows = d.build_rows(cohort, recs3, cache2)
check(len(call_rows) == 2 and len(none_rows) == 2, "2 call rows (records) + 2 no-phone rows (no callable, no record)")
dup = cohort + [{**cohort[0], "rank": 4, "dataflik_id": "9"}]
cr, pr, nr = d.build_rows(dup, recs3, cache2)
check(len(cr) == 2 and len(pr) == 6 and any(n["reason"].startswith("duplicate of rank 1") for n in nr),
      "a second row on the same CRM record is a duplicate, not a second set of calls")
r1 = call_rows[0]
check(r1["phone_1"] == "8655550112" and r1["phone_1_tier"] == "Dial First" and r1["phone_1_display"] == "(865) 555-0112",
      "slot 1 is the best phone")
check(r1["phone_5"] == "8655550110" and r1["more_phones"] == 1 and r1["callable_phones"] == 6, "5 slots + overflow count")
check(r1["owner_type"] == "Absentee" and call_rows[1]["owner_type"] == "Company", "owner type: label vs company")
check(call_rows[1]["est_value"] == 100000, "cohort est value fills when the record has none")
check(call_rows[1]["source"] == "new" and r1["source"] == "existing", "source flag from pulled")
check(len(phone_rows) == 6 and phone_rows[0]["owner_name"] == "Jane Doe", "phone rows one per callable phone with owner name")
check(r1["crm_record_url"].endswith("/records/u1"), "CRM link")

with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "x.csv"
    d.write_csv(p, [{**r1, "known_distress": "tired — landlord – x"}], d.CALL_FIELDS)
    txt = p.read_text(encoding="utf-8")
    check("—" not in txt and "–" not in txt, "no em or en dash in the CSV")
    with p.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    check(rows[0]["phone_1"] == "8655550112" and list(rows[0].keys()) == d.CALL_FIELDS, "csv round trip + column order")

drows, ddrop, dtrunc, dsup = d.dialer_rows(cohort, recs3, cache2, {"Dial First", "Dial Second"}, 2)
check(len(drows) == 1 and ddrop == 1 and dtrunc == 1, "dialer: one property kept, company-only dropped (WRONG), 3 D1/D2 numbers truncated to 2")
check(drows[0]["Phone 1"] == "8655550112" and drows[0]["Phone 2"] == "8655550111" and drows[0]["First Name"] == "Jane"
      and list(drows[0].keys()) == d.DIALER_BASE + ["Phone 1", "Phone 2"], "dialer layout and ordering")
drows2, _, _, _ = d.dialer_rows(cohort[1:2], {"u2": {**recs3["u2"], "phones": [ph("8655550120", tier="Dial First")]}}, {}, {"Dial First"}, 1)
drows3, _, _, dsup3 = d.dialer_rows(cohort, {**recs3, "u1": {**recs3["u1"], "lists": ["Free & Clear", "obituary"]}},
                                     cache2, {"Dial First"}, 2, {"Obituary"})
check(len(drows3) == 0 and dsup3 == 1, "a record on the Obituary list is suppressed (case-insensitive)")
check(drows2[0]["Last Name"] == "Acme LLC" and drows2[0]["First Name"] == "", "company lands in Last Name when no person")
krows, kd, ks, kt, kf = d.kixie_rows(cohort + [{**cohort[1], "rank": 5, "dataflik_id": "7", "saved_uuid": "u3"}],
                                 {**recs3, "u3": {**recs3["u2"], "phones": [ph("8655550130", tier="Dial First"), ph("8655550131", tier="Dial Second")]}},
                                 cache2, {"Dial First", "Dial Second"}, {"Obituary"}, phone_cols=2)
check(len(krows) == 2 and kd == 1 and ks == 0 and kt == 1, "kixie: one row per property, WRONG-only dropped, one truncated")
check([krows[0]["Phone 1"], krows[0]["Phone 2"]] == ["18655550112", "18655550111"], "Phone 1/2 best first, E.164 without plus")
check(krows[0]["Dial Tiers"] == "First, First" and "1 more in CRM" in krows[0]["PowerList Notes"], "tiers listed, overflow noted")
check(krows[1]["Last Name"] == "Acme LLC" and krows[1]["Company Name"] == "Acme LLC" and krows[1]["Title"] == "Company"
      and krows[1]["Phone 2"] == "18655550131", "company row: name, title, second number")
check(list(krows[0].keys()) == d.KIXIE_FIELDS[:2] + d.KIXIE_FIELDS[5:] or set(krows[0]) == set(d.KIXIE_FIELDS[:2] + d.KIXIE_FIELDS[5:]),
      "kixie layout keys")
ko, _, _, _, _ = d.kixie_rows(cohort, {**recs3, "u1": {**recs3["u1"], "lists": ["obituary"]}}, cache2, {"Dial First"}, {"Obituary"})
check(len(ko) == 0, "kixie suppresses Obituary records")
shared = [ph("8655550140", tier="Dial First"), ph("8655550141", tier="Dial Second")]
dd_cohort = [dict(cohort[0], saved_uuid="a1"), dict(cohort[1], rank=2, saved_uuid="a2"), dict(cohort[1], rank=3, dataflik_id="8", saved_uuid="a3")]
dd_recs = {"a1": {**recs3["u1"], "phones": shared}, "a2": {**recs3["u1"], "street": "2 Main St", "phones": shared},
           "a3": {**recs3["u1"], "street": "3 Main St", "phones": shared + [ph("8655550142", tier="Dial First")]}}
dr, _, _, _, df = d.kixie_rows(dd_cohort, dd_recs, {}, {"Dial First", "Dial Second"})
check(len(dr) == 2 and df == 1, "dedupe: a row whose numbers were all dialed higher up is folded: %d rows, %d folded" % (len(dr), df))
check(dr[0]["Phone 1"] == "18655550140" and "also owns: 2 Main St" in dr[0]["PowerList Notes"], "folded address lands in the surviving row's notes")
check(dr[1]["Phone 1"] == "18655550142" and dr[1]["Phone 2"] == "", "a row keeps only the numbers not dialed higher up")
dr2, _, _, _, df2 = d.kixie_rows(dd_cohort, dd_recs, {}, {"Dial First", "Dial Second"}, dedupe=False)
check(len(dr2) == 3 and df2 == 0, "--no-dedupe keeps every row")
check(d.CALL_FIELDS.count("phone_5_score") == 1 and "property_id" in d.CALL_FIELDS, "call list columns present")
check("phone_display" in d.PHONE_FIELDS and "tier" in d.PHONE_FIELDS, "phone list columns present")

print("\n%d checks, %d failed" % (0, len(FAILS)) if False else "")
if FAILS:
    print("FAILED: %d" % len(FAILS))
    sys.exit(1)
print("all checks passed")
