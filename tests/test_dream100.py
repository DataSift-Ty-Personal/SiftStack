"""Zero-network checks on the Dream 100 gates and score. python tests/test_dream100.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import dream100_build as b  # noqa: E402

REC = {
    "uuid": "u1", "dataflik_id": 1, "status": None, "lists": ["D4D Top 3000", "Vacant", "Tax Delinquent"],
    "tags": ["D4D"], "address": {"street": "1 Main St", "city": "Knoxville", "postal_code": "37917", "state": "TN",
                                 "county": "Knox", "vacant": True},
    "owner": {"uuid": "o1", "type": "person", "first_name": "Ann", "last_name": "Lee", "age": 80,
              "address": {"street": "9 Elm St", "city": "Knoxville", "state": "TN", "postal_code": "37919"},
              "phones": [{"number": "8655551212", "type": "MOBILE", "tags": ["Dial First"], "status": "UNKNOWN"}],
              "emails": []},
    "estimate_value": "300000", "equity_percent": "100", "last_sale_price": "50000", "last_sold": "1999-01-01",
    "rental_value": "1500", "structure_type": "Single Family Residential", "mls": "Off Market",
    "investor_score": 90, "realtor_score": 20, "on_market_score": 50, "tax_delinquent_value": "1200",
}
CO = {"rank": 3, "score": 100, "known_distress": "vacant"}
n = 0


def check(cond, msg):
    global n
    n += 1
    assert cond, msg


r = b.score(b.flatten(REC, CO, {}))
check(b.gate_reason(r) is None, "a clean record passes the gates")
check(r["deal_size"] == 90000, "discount room is 30%% of value when equity covers it: %s" % r["deal_size"])
check(r["pts"]["cond"] == 20 and r["pts"]["inv"] == 20 and r["pts"]["reach"] == 10, r["pts"])
check(r["evidence_raw"] == 13 and r["pts"]["evid"] == 13.9, "vacant 4 + tax delinquent 4 + tax owed 3 + age 1 + held 20+ 1 = 13 raw: %s" % r["pts"])
check(80 < r["opp"] <= 100, r["opp"])
check(chr(0x2014) not in r["why"] and chr(0x2013) not in r["why"], "no dashes in prose")
check(b.no_dash("a " + chr(0x2014) + " b " + chr(0x2013) + " 2014") == "a - b - 2014", "no_dash strips dashes and leaves years alone")

# equity caps the room
low = dict(REC, equity_percent="10")
r2 = b.score(b.flatten(low, CO, {}))
check(r2["deal_size"] == 30000, "room capped by equity: %s" % r2["deal_size"])
check("capped by equity" in r2["why"], r2["why"])

# gates
check(b.gate_reason(b.flatten(dict(REC, status="sold"), CO, {})) == "Lead status sold", "dead status")
check(b.gate_reason(b.flatten(dict(REC, estimate_value="800000"), CO, {})).startswith("Over the"), "buy box")
check(b.gate_reason(b.flatten(dict(REC, equity_percent="-5"), CO, {})).startswith("Upside down"), "upside down")
check(b.gate_reason(b.flatten(dict(REC, lists=["Dispo - VIP Buyers"]), CO, {})).startswith("On a Dispo"), "buyer list")
check(b.gate_reason(b.flatten(dict(REC, structure_type="Residential (General) (Single)"), CO, {})) is None, "SFR variant passes")
check(b.gate_reason(b.flatten(dict(REC, structure_type="Duplex (2 units)"), CO, {})).startswith("Not single"), "duplex drops")
check(b.gate_reason(b.flatten(dict(REC, mls="Listed"), CO, {})).startswith("Listed"), "MLS listed drops")

# realtor penalty and DNC
r3 = b.score(b.flatten(dict(REC, realtor_score=100), CO, {}))
check(r3["pts"]["inv"] == 15, "realtor 100 costs 5: %s" % r3["pts"])
dnc = dict(REC, owner=dict(REC["owner"], dnc=True))
r4 = b.score(b.flatten(dnc, CO, {}))
check(r4["pts"]["reach"] == 0 and r4["reach"].startswith("DNC"), r4["reach"])

# a disposed number is skipped, an untagged number takes its tier from the score cache
wrong = dict(REC, owner=dict(REC["owner"], phones=[
    {"number": "8655551212", "type": "MOBILE", "tags": [], "status": "WRONG"},
    {"number": "8655559999", "type": "LANDLINE", "tags": [], "status": "UNKNOWN"}]))
r5 = b.score(b.flatten(wrong, CO, {"8655559999": {"tier": "Dial Third"}}))
check(r5["best_tier"] == "Dial Third" and len(r5["phones"]) == 1, r5["phones"])
print("test_dream100: %d checks passed" % n)
