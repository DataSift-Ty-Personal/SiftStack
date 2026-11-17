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

# ---- the named weight sets (decision D31): today's is the default, the taught one is by name ----
import os  # noqa: E402
import tempfile  # noqa: E402

import dream100 as d  # noqa: E402

shipped = (b.W_DEAL, b.W_COND, b.W_INV, b.W_EVID, b.W_REACH, dict(b.TIER_PTS))
check(d.DEFAULT_WEIGHTS == "today" and d.weights_name() == "today", "today's weights are the default")
check(d.WEIGHT_SETS["today"] == {"deal": b.W_DEAL, "cond": b.W_COND, "inv": b.W_INV, "evid": b.W_EVID, "reach": b.W_REACH},
      "the today set restates the scoring module's own numbers: %s" % d.WEIGHT_SETS["today"])
check({t: 10 * s for t, s in d.TIER_SHARE.items()} == {t: float(p) for t, p in b.TIER_PTS.items()},
      "the tier shares are today's 10, 7, 4 and 2 of 10")
check(all(sum(w.values()) == 100 for w in d.WEIGHT_SETS.values()), "every weight set adds to 100 points")
d.apply_weights(None, b)
check((b.W_DEAL, b.W_COND, b.W_INV, b.W_EVID, b.W_REACH, b.TIER_PTS) == shipped, "applying the default changes nothing")
check(b.score(b.flatten(REC, CO, {}))["opp"] == r["opp"], "and the score of a record is what it was")

# a big deal nobody can reach against a smaller one with a Dial First number
BIG = dict(REC, uuid="u-big", estimate_value="600000", owner=dict(REC["owner"], phones=[]))
SMALL = dict(REC, uuid="u-small", estimate_value="200000")


def opp(rec):
    return b.score(b.flatten(rec, CO, {}))


big, small = opp(BIG), opp(SMALL)
check(big["reach"] == "no phone" and big["pts"]["reach"] == 0 and big["pts"]["deal"] == 35, big["pts"])
check(big["opp"] > small["opp"], "today the unreachable big deal outranks the reachable one: %s vs %s" % (big["opp"], small["opp"]))

w = d.apply_weights("taught", b)
check(w == {"reach": 35, "deal": 25, "cond": 15, "inv": 15, "evid": 10}, "the taught set: reach first, then deal size")
check((b.W_REACH, b.W_DEAL) == (35, 25) and b.W_REACH > b.W_DEAL > max(b.W_COND, b.W_INV, b.W_EVID),
      "a reachable number weighs most, deal size second")
check(b.TIER_PTS == {"Dial First": 35.0, "Dial Second": 24.5, "Dial Third": 14.0, "Dial Fourth": 7.0, "Drop": 0.0, "": 0.0},
      "the dial tiers keep their shares of the reach points: %s" % b.TIER_PTS)
big_t, small_t = opp(BIG), opp(SMALL)
check(small_t["pts"]["reach"] == 35 and big_t["pts"]["reach"] == 0, "Dial First earns all 35, no phone earns none")
check(small_t["opp"] > big_t["opp"], "taught: the deal that can be reached outranks the bigger one that cannot: %s vs %s" % (
    small_t["opp"], big_t["opp"]))
check(0 < small_t["opp"] <= 100 and 0 < big_t["opp"] <= 100, "scores stay on 100 points")
dnc_t = opp(dnc)
check(dnc_t["pts"]["reach"] == 0 and dnc_t["reach"].startswith("DNC"), "a do-not-call owner still earns no reach points")

# a workbook built on the taught weights states them, not today's
row = dict(small_t, owner_other=0)
with tempfile.TemporaryDirectory() as tmp:
    from pathlib import Path as _P
    from openpyxl import load_workbook
    book = _P(tmp) / "Dream100_test.xlsx"
    b.write_workbook([row], [], [row], [], [], book, 1)
    before = [c for ws in load_workbook(book).worksheets[:2] for r_ in ws.iter_rows(values_only=True) for c in r_
              if isinstance(c, str)]
    check(any("deal size 35, condition 20, investor 20, evidence 15, reach 10" in c for c in before),
          "as built, the sheet states today's weights whatever it was scored on")
    check(d.restate_workbook(book, "taught") == [], "every statement of the weights was found and restated")
    after = [c for ws in load_workbook(book).worksheets[:2] for r_ in ws.iter_rows(values_only=True) for c in r_
             if isinstance(c, str)]
    check(any("deal size 25, condition 15, investor 15, evidence 10, reach 35" in c for c in after), "the subtitle states the taught weights")
    check("Reach (35)" in after and "Deal size (25)" in after and "Reach (10)" not in after, "the Overview names each weight")
    check(any("Dial First 35, Second 24.5, Third 14, Fourth 7" in c for c in after), "and the points each dial tier earns")
    check(d.restate_workbook(book, "taught") == [], "restating twice finds it already right")
    check(d.restate_workbook(book, "today") == [], "today's set has nothing to restate")
    plain = _P(tmp) / "plain.xlsx"
    from openpyxl import Workbook
    wb_ = Workbook()
    wb_.active["A1"] = "a sheet that states no weights"
    wb_.save(plain)
    check(len(d.restate_workbook(plain, "taught")) == 7, "a sheet whose wording changed is reported, never trusted")

# both sets side by side, from the same records, and the module is put back afterwards
full = {"u-big": BIG, "u-small": SMALL, "u1": REC}
cmp_ = d.compare(full, {}, {}, top=1, build=b)
check(cmp_["today"]["uuids"] != cmp_["taught"]["uuids"] and cmp_["same"] == 0, "the top row differs between the two sets")
check(cmp_["today"]["reachable"] == 0 and cmp_["taught"]["reachable"] == 1, "taught puts a reachable owner on top: %s" % cmp_)
check(cmp_["leave"][0]["street"] == "1 Main St" and cmp_["leave"][0]["reach"] == "no phone", cmp_["leave"])
check((b.W_DEAL, b.W_COND, b.W_INV, b.W_EVID, b.W_REACH, b.TIER_PTS) == shipped, "compare leaves the module on today's weights")

d.apply_weights("taught", b)
d.apply_weights("today", b)
check((b.W_DEAL, b.W_COND, b.W_INV, b.W_EVID, b.W_REACH, b.TIER_PTS) == shipped, "today puts the module's own numbers back exactly")
check(opp(BIG)["opp"] == big["opp"] and opp(SMALL)["opp"] == small["opp"], "and scores read as they did before")

# chosen by name only: an unknown name is refused, the environment can set the default
try:
    d.apply_weights("biggest", b)
    check(False, "an unknown weight set is refused")
except ValueError as e:
    check("today" in str(e) and "taught" in str(e), str(e))
check(d.out_dir_for("today") == d.OUT and d.out_dir_for("taught") == d.OUT / "taught",
      "a set that is not the default writes into its own folder")
os.environ[d.WEIGHTS_ENV] = "taught"
try:
    check(d.weights_name() == "taught" and d.out_dir_for() == d.OUT and d.out_dir_for("today") == d.OUT / "today",
          "the environment names the default, and the default owns the main folder")
finally:
    os.environ.pop(d.WEIGHTS_ENV, None)
check(d.weights_name() == "today", "with nothing set, today's")
for name in ("src/dream100.py", "tests/test_dream100.py"):
    text = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
    check(chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text, "no long dash or backspace in %s" % name)
print("test_dream100: %d checks passed" % n)
