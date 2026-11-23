"""Zero-network tests for src/mls_offers.py. Invented streets and agents only.

    python tests/test_mls_offers.py      (or pytest)
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import mls_offers as m  # noqa: E402

TODAY = date(2026, 11, 15)
POL = m.Policy()


def ms(d: str) -> int:
    y, mo, dd = map(int, d.split("-"))
    from datetime import datetime, timezone
    return int(datetime(y, mo, dd, tzinfo=timezone.utc).timestamp() * 1000)


def listing(**kw):
    base = {"id": 1, "address": "100 Example St", "city": "Knoxville", "zip": "37917", "state": "TN", "value": 200_000,
            "rent": 1_800, "sqft": 1_200, "year": 1960, "owner": "Pat Example", "corporate": False, "flags": [],
            "has_address": True, "list_price": 200_000, "listed_since": "2026-05-01", "agent": "Sam Agent",
            "agent_phone": "(865) 000-0001", "office": "Example Realty", "loans": [], "tax": 1_200,
            "lat": 35.98, "lon": -83.92}
    base.update(kw)
    return base


def ev(lat=35.98, lon=-83.92, psf=80, sqft=1_200, typ="Pending", value=None, **kw):
    price = psf * sqft
    return {"id": kw.get("id", 9), "lat": lat, "lon": lon, "price": price, "psf": psf, "type": typ, "sqft": sqft,
            "value": value if value is not None else price * 1.3, "buyer": kw.get("buyer", "Example Homes LLC"),
            "buyer_key": kw.get("buyer_key"), "address": "1 Other St", "zip": "37917"}


def test_history_counts_cuts_relists_and_run_after_last_sale():
    rows = [
        {"recording_date": ms("2021-03-01"), "status": "Sold", "sale_price": 150_000, "listing_date": ms("2021-01-01")},
        {"recording_date": ms("2026-01-02"), "status": "Active", "sale_price": 300_000, "listing_date": ms("2026-01-01"), "agent_name": "A One"},
        {"recording_date": ms("2026-02-01"), "status": "Active", "sale_price": 300_000, "listing_date": ms("2026-01-01"), "agent_name": "A One"},
        {"recording_date": ms("2026-03-01"), "status": "Active", "sale_price": 280_000, "listing_date": ms("2026-01-01"), "agent_name": "A One"},
        {"recording_date": ms("2026-06-01"), "status": "Fail", "sale_price": 280_000, "listing_date": ms("2026-01-01"), "agent_name": "A One"},
        {"recording_date": ms("2026-07-01"), "status": "Active", "sale_price": 260_000, "listing_date": ms("2026-01-01"), "agent_name": "B Two"},
        {"recording_date": ms("2026-08-01"), "status": "Pending", "sale_price": 260_000, "listing_date": ms("2026-01-01"), "agent_name": "B Two"},
        {"recording_date": ms("2026-09-01"), "status": "Active", "sale_price": 255_000, "listing_date": ms("2026-01-01"), "agent_name": "B Two"},
    ]
    h = m.summarize_history(rows, 255_000, TODAY)
    assert h["original_price"] == 300_000          # the 2021 sale is not part of this run
    assert h["cuts"] == 3                           # a repeated price is not a cut
    assert h["relists"] == 1 and h["fell_through"] == 1 and h["agents"] == 2
    assert abs(h["cut_pct"] - 0.15) < 1e-9
    assert h["dom"] == (TODAY - date(2026, 1, 1)).days


def test_portfolio_deed_and_tiny_house_are_not_evidence():
    src = {"location": "POINT (-83.9 35.9)", "last_sale_price": 1_285_200, "living_area_sqft": 1_134,
           "property_value": 180_000, "last_sale_investor_transaction_type": "Pending"}
    assert m.slim_evidence(src) is None             # twice the parcel's value or more = a takedown deed
    src.update(last_sale_price=90_000)
    assert m.slim_evidence(src)["psf"] == round(90_000 / 1_134, 1)
    assert m.slim_evidence({**src, "living_area_sqft": 300}) is None
    assert m.parse_point("POINT (-83.84119 35.977127)") == (35.977127, -83.84119)


def test_buyer_market_prefers_discounted_buys_and_widens_radius():
    row = listing()
    far = [ev(lat=35.98 + 0.012, psf=70, value=200_000) for _ in range(3)]       # about 0.83 mi north
    assert m.buyer_market(row, far, set())["inv_radius"] == 1.0
    near = [ev(psf=60, value=200_000), ev(psf=62, value=200_000), ev(psf=64, value=200_000),
            ev(psf=140, value=170_000), ev(psf=150, value=170_000)]                 # two turnkey buys at full value
    bm = m.buyer_market(row, near, set())
    assert bm["inv_radius"] == 0.5 and bm["inv_psf"] == 62 and bm["investor_price"] == 62 * 1_200
    big = [ev(sqft=3_000, psf=60) for _ in range(5)]                                  # not the same size of house
    assert m.buyer_market(row, big, set())["inv_n"] == 0


def test_reachable_buyers_counted_from_registry_keys():
    near = [ev(buyer="Alpha Homes LLC", buyer_key="ALPHA HOMES"), ev(buyer="Alpha Homes LLC", buyer_key="ALPHA HOMES"),
            ev(buyer="Beta Props LLC", buyer_key="BETA PROPS")]
    bm = m.buyer_market(listing(), near, {"ALPHA HOMES"})
    assert bm["buyers_near"] == 2 and bm["reachable_near"] == 1
    assert bm["top_buyers"].startswith("Alpha Homes Llc (phone)")


def test_cash_offer_from_buyers_capped_at_70_pct_of_list():
    row = listing(list_price=150_000)
    bm = {"investor_price": 140_000, "inv_basis": "x", "arv": None}
    c = m.cash_offer(row, 10, POL, bm)
    assert c["cash_offer"] == 105_000 and "70% of list" in c["cash_basis"]     # buyers would pay more: hold the anchor
    bm = {"investor_price": 100_000, "inv_basis": "x", "arv": None}
    c = m.cash_offer(row, 10, POL, bm)
    assert c["cash_offer"] == 85_000 and "buyers pay" in c["cash_basis"]        # buyer price less the fee
    assert c["spread_at_list_rule"] == 100_000 - 105_000                         # 70% of list would lose money


def test_cash_lowball_goes_to_price_watch_with_trigger():
    c = m.cash_offer(listing(list_price=220_000), 10, POL, {"investor_price": 76_000, "inv_basis": "x", "arv": None})
    assert c["cash_offer"] is None and c["watch_offer"] == 61_000 and c["watch_trigger"] == 122_000


def test_cash_cap_and_avm_fallback():
    assert m.cash_offer(listing(list_price=400_000), 10, POL, {})["cash_offer"] is None
    c = m.cash_offer(listing(list_price=150_000, value=200_000), 10, POL, {})
    assert c["cash_offer"] == m.round_down(200_000 * 0.60 - 1_200 * 15 - 15_000)


def test_subject_to_low_equity_pays_the_gap_and_the_commission():
    row = listing(list_price=200_000, loans=[{"type": "FHA", "rate": 3.0, "balance": 192_000, "payment": 900}], rent=2_200)
    c = m.creative_offer(row, POL)
    assert c["lane"] == "Subject-to" and c["cash_to_seller"] == 8_000
    assert c["cash_in"] == round(8_000 + 200_000 * 0.025 + 200_000 * 0.02)
    assert c["coc"] and c["coc"] > POL.creative_min_coc


def test_subject_to_carry_no_equity_and_too_much_equity():
    loan = lambda b, r=3.2: [{"type": "Conventional", "rate": r, "balance": b, "payment": 700}]
    carry = m.creative_offer(listing(list_price=200_000, rent=2_400, loans=loan(160_000)), POL)
    assert carry["lane"] == "Subject-to + seller carry" and carry["seller_carry"] == 28_000
    under = m.creative_offer(listing(list_price=200_000, rent=2_400, loans=loan(210_000)), POL)
    assert under["lane"] == "Subject-to (no equity)" and under["cash_to_seller"] == 0
    rich = m.creative_offer(listing(list_price=200_000, rent=2_400, loans=loan(80_000)), POL)
    assert rich["lane"] == "Subject-to + seller carry" and rich["seller_carry"] == 108_000   # Ty: every good rate
    assert rich["carry_payment"] == 300 and rich["piti"] == round(700 + 100 + 200_000 * 0.0065 / 12 + 300)
    capped = m.Policy(subto_max_carry_pct=0.20)                                              # his own narrower rule
    assert "cash lane fits better" in m.creative_offer(listing(list_price=200_000, rent=2_400, loans=loan(80_000)), capped)["creative_note"]
    high = m.creative_offer(listing(list_price=200_000, rent=2_400, loans=loan(190_000, 7.1)), POL)
    assert high["lane"] == "" and "keep line" in high["creative_note"]


def test_credit_line_never_qualifies_and_no_loan_needs_the_free_and_clear_flag():
    heloc = m.creative_offer(listing(loans=[{"type": "Credit Line", "rate": 6.0, "balance": 50_000, "payment": 300}]), POL)
    assert heloc["lane"] == "" and "credit line" in heloc["creative_note"].lower()
    assert m.creative_offer(listing(loans=[]), POL)["lane"] == ""
    fc = m.creative_offer(listing(loans=[], flags=["is_free_and_clear"], list_price=120_000, rent=1_600), POL)
    assert fc["lane"] == "Seller finance"


def test_creative_that_does_not_cash_flow_is_dropped():
    c = m.creative_offer(listing(loans=[], flags=["is_free_and_clear"], list_price=300_000, rent=1_200), POL)
    assert c["lane"] == "" and c["dropped_lane"] == "Seller finance"


def test_weak_subject_to_is_flagged_not_dropped():
    loans = [{"type": "Conventional", "rate": 3.0, "balance": 250_000, "payment": 1_400}]
    c = m.creative_offer(listing(list_price=300_000, rent=1_500, loans=loans), POL)
    assert c["lane"].startswith("Subject-to") and c.get("weak_cash_flow") and "wrap or a resale" in c["creative_note"]
    strict = m.creative_offer(listing(list_price=300_000, rent=1_500, loans=loans), m.Policy(subto_gate_cash_flow=True))
    assert strict["lane"] == ""


def test_mortgage_summary_every_row():
    loans = [{"type": "Conventional", "rate": 3.0, "balance": 150_000, "payment": 800, "recorded": "2020-04-01"},
             {"type": "Credit Line", "rate": 7.0, "balance": 50_000, "payment": 300, "recorded": "2024-02-01"}]
    s = m.mortgage_summary(listing(loans=loans, total_balance=200_000, total_payment=1_100))
    assert s["mtg_balance"] == 200_000 and s["mtg_payment"] == 800        # the credit line is a payoff, not a payment
    assert s["mtg_rate"] == 3.0                                  # the credit line does not drag the rate
    assert s["mtg_detail"].startswith("Conventional 3.00% (index estimate) $150,000, $800 P&I from 2020")
    assert s["mtg_full"] == round(800 + 1_200 / 12 + 200_000 * 0.0065 / 12)
    modeled = m.overlay_model(listing(loans=loans), [
        {"recorded": "2020-04-01", "type": "Conventional", "rate": 3.25, "balance": 130_000, "pi": 650, "mi": 0,
         "rate_source": "HMDA match", "term_months": 360, "original": 161_000, "paid_off": False},
        {"recorded": "2024-02-01", "type": "Credit Line"}])
    s2 = m.mortgage_summary(modeled)
    assert s2["mtg_rate"] == 3.25 and s2["mtg_balance"] == 180_000 and s2["mtg_payment"] == 650
    assert "3.25% (HMDA match) $130,000 left of $161,000, $650 P&I, 30-yr from 2020 (index said 3.00%)" in s2["mtg_detail"]
    gone = m.overlay_model(listing(loans=loans[:1]), [{"recorded": "2020-04-01", "type": "Conventional", "rate": 3.0,
                                                       "paid_off": True, "balance": 0, "pi": 0, "mi": 0}])
    assert gone["loans"] == [] and "amortizes to zero" in gone["model_notes"][0]
    assert "limit, not payoff" in s["mtg_detail"]
    assert m.mortgage_summary(listing(flags=["is_free_and_clear"]))["mtg_balance"] == 0
    assert m.mortgage_summary(listing())["mtg_balance"] is None  # no loan on record is not the same as $0


def test_off_market_ai_score_adds_up_to_its_weight():
    h = m.summarize_history([], 200_000, TODAY)
    base, _ = m.motivation(listing(ai_off=20), h)
    mid, _ = m.motivation(listing(ai_off=60), h)
    top, why = m.motivation(listing(ai_off=95), h)
    assert base == 0 and mid == round(15 * 30 / 60) and top == 15
    assert "AI off-market score 95" in why
    assert m.motivation(listing(ai_off=95), h, pol=m.Policy(ai_weight=0))[0] == 0


def test_owner_type_reads_the_name_before_the_flags():
    t = lambda **kw: m.owner_type(listing(**{"corporate": False, "trust": False, **kw}))
    assert t(owner="Pruitt Lyle D & Coralee M", corporate=True) == "Individual"    # the index flag is wrong here
    assert t(owner="Example Flips Llc") == "Company" and t(owner="Opendoor Property Trust") == "Company"
    assert t(owner="Pat Example Revocable Living Trust") == "Trust"
    assert t(owner="Estate Of Pat Example") == "Estate"
    assert t(owner="Secretary Of Veterans Affairs") == "Bank or government"
    assert t(owner="Example Financial Services Llc") == "Bank or government"
    assert t(owner="", corporate=True) == "Company" and t(owner="", trust=True) == "Trust" and t(owner="") == "Unknown"


def test_owed_share_of_list_keeps_credit_lines_apart():
    loans = [{"type": "FHA", "rate": 3.0, "balance": 180_000, "payment": 900},
             {"type": "Credit Line", "rate": 7.0, "balance": 30_000, "payment": 200}]
    s = m.mortgage_summary(listing(list_price=200_000, loans=loans))
    assert s["owed_pct"] == 0.9 and s["owed_pct_all"] == 1.05
    s = m.mortgage_summary(listing(list_price=200_000, loans=loans[:1]))
    assert s["owed_pct"] == 0.9 and s["owed_pct_all"] is None
    assert m.mortgage_summary(listing(flags=["is_free_and_clear"])).get("owed_pct") is None


def test_join_pairs_same_day_first_and_second_loans():
    rec = [{"amount": 167_259, "recorded": date(2021, 10, 4), "due": date(2051, 10, 1), "lender": "A"},
           {"amount": 7_500, "recorded": date(2021, 10, 4), "due": None, "lender": "A"}]
    used = set()
    first = m.join_loan({"recorded": "2021-10-04", "balance": 152_193}, rec, used)
    second = m.join_loan({"recorded": "2021-10-04", "balance": 6_824}, rec, used)
    assert first["amount"] == 167_259 and second["amount"] == 7_500       # each record used once, by amount
    assert m.join_loan({"recorded": "2021-10-04", "balance": 400_000}, rec, set()) is None   # no plausible record


def test_construction_loan_is_a_payoff_not_a_payment():
    loans = [{"type": "Conventional", "rate": 3.76, "balance": 430_000, "payment": 2_188, "recorded": "2022-04-13"},
             {"type": "Construction", "rate": 6.3, "balance": 133_000, "payment": 17_068, "recorded": "2026-06-10"}]
    s = m.mortgage_summary(listing(list_price=660_000, value=690_000, loans=loans))
    assert s["mtg_payment"] == 2_188 and s["owed_pct"] == round(430_000 / 660_000, 3)
    assert s["owed_pct_all"] == round(563_000 / 660_000, 3) and "paid off at the sale" in s["mtg_detail"]
    c = m.creative_offer(listing(list_price=660_000, value=690_000, rent=3_500, loans=loans), POL)
    assert c["piti"] < 4_000                                                # the construction payment stays out
    assert "construction loan of record" in c["creative_note"]
    assert m.owner_type(listing(owner="Rcf 2 Acquisition Trust", corporate=True, trust=True)) == "Company"


def test_blanket_or_oversized_loan_gets_no_subject_to():
    loans = [{"type": "Conventional", "rate": 3.0, "balance": 600_000, "payment": 2_500, "recorded": "2021-01-01"}]
    big = m.creative_offer(listing(list_price=200_000, rent=2_000, loans=loans), POL)
    assert big["lane"] == "" and "blanket or portfolio loan" in big["creative_note"]
    shared = m.creative_offer(listing(list_price=700_000, rent=4_000, loans=loans, blanket=2), POL)
    assert shared["lane"] == "" and "blanket" in m.mortgage_summary(listing(list_price=700_000, loans=loans, blanket=2))["mtg_detail"]
    heloc_only_pay = m.mortgage_summary(listing(loans=[{"type": "Credit Line", "rate": 7.0, "balance": 100_000, "payment": 2_000}]))
    assert heloc_only_pay["mtg_payment"] is None                      # a credit line is a payoff, not a payment


def test_screen_reasons():
    assert m.screen(listing(list_price=900_000), POL, today=TODAY).startswith("list over")
    assert m.screen(listing(year=2026), POL, today=TODAY) == "new construction"
    assert m.screen(listing(address="0 Example St"), POL, today=TODAY) == "no street address"
    assert m.screen(listing(), POL, {"status": "PENDING"}, TODAY).startswith("no longer active")
    assert m.screen(listing(), POL, today=TODAY) is None


def test_offer_text_is_plain_and_honest():
    row = {**listing(), "cash_offer": 120_000, "lane": "Subject-to", "creative_price": 200_000, "cash_to_seller": 8_000,
           "seller_carry": None, "agent": "J. Sam Agent"}
    t = m.offer_text(row, "Ty")
    assert t.startswith("Hi Sam, this is Ty, a local investor.")
    assert "Or I can pay $200,000" in t and t.count("?") == 1
    assert chr(0x2014) not in t and chr(0x2013) not in t and "pre-approved" not in t.lower()
    only = m.offer_text({**row, "lane": ""})
    assert only.startswith("Hi Sam, I'm a local investor.")          # nobody named unless a sender is set
    assert only.endswith("Would your seller consider it?")


def test_agents_roster_groups_by_phone():
    rows = [{**listing(), "score": 60}, {**listing(address="200 Example St"), "score": 20},
            {**listing(agent="Other Person", agent_phone="(865) 000-0002"), "score": 10}]
    roster = m.agents_roster(rows)
    assert roster[0]["listings"] == 2 and roster[0]["motivated"] == 1 and len(roster) == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
