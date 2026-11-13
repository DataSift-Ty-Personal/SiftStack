"""County tax on the Knox lien records: delinquent, not merely unpaid.

Zero network, invented data. The county's `amount_due` is every unpaid bill,
including the current year's, which is not late until the end of February. In
November 2026 that would have tagged 1,274 of 1,451 lien parcels as tax
delinquent.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_ftm_pull as kp  # noqa: E402
import tax_enricher as te  # noqa: E402


def _rec(account="010AA-001", amount_due=1840.22, years=(2026,)):
    return {"tax_account": account, "tax_due": amount_due, "tax_years": list(years),
            "notes": "Lien debtor DOE PAT: 1 lien instrument(s) recorded (LEN)."}


def test_a_current_year_bill_is_not_a_delinquency():
    r = _rec()
    stats = kp.apply_true_delinquency(
        [r], lookup=lambda a: {"delinquent_amount": 0.0, "delinquent_years": 0,
                               "delinquent_year_list": []}, sleep=0)
    assert kp._tax_due(r) == "" and kp._tax_year(r) == ""
    assert r["notes"].startswith("No delinquent county tax.")
    assert stats == {"checked": 1, "delinquent": 0, "unreadable": 0}


def test_a_real_delinquency_carries_its_amount_and_oldest_year():
    r = _rec()
    kp.apply_true_delinquency(
        [r], lookup=lambda a: {"delinquent_amount": 2310.5, "delinquent_years": 2,
                               "delinquent_year_list": ["2024", "2025"]}, sleep=0)
    assert kp._tax_due(r) == 2310.5 and kp._tax_year(r) == "2024"
    assert r["notes"].startswith("Delinquent county tax $2,310.50 for 2024, 2025.")


def test_unreadable_bills_leave_the_figure_blank_not_the_raw_amount():
    r = _rec()
    stats = kp.apply_true_delinquency([r], lookup=lambda a: None, sleep=0)
    assert kp._tax_due(r) == "" and r["tax_years"] == []
    assert "could not be read" in r["notes"] and stats["unreadable"] == 1


def test_records_without_a_tax_account_are_left_alone():
    # A condemnation row reads its tax figure from the agenda prose instead.
    r = {"tax_due": 271.36, "tax_years": [2025], "notes": "agenda text"}
    stats = kp.apply_true_delinquency([r], lookup=lambda a: 1 / 0, sleep=0)
    assert r == {"tax_due": 271.36, "tax_years": [2025], "notes": "agenda text"}
    assert stats["checked"] == 0


def test_the_delinquent_sum_feeds_total_delinquency_not_the_raw_amount():
    r = _rec(amount_due=1840.22)
    r.update({"lien_total": 5000.0, "sm": {"total_market_value": 200000}})
    kp.apply_true_delinquency(
        [r], lookup=lambda a: {"delinquent_amount": 0.0, "delinquent_years": 0,
                               "delinquent_year_list": []}, sleep=0)
    assert kp._equity_block(r)["Total Delinquency"] == 5000.0


def test_parse_delinquency_lists_only_late_unpaid_years():
    bills = [
        {"taxYear": "2026", "due": 1840.22, "delinquent": False, "paid": False},   # current, not late
        {"taxYear": "2025", "due": 1200.00, "delinquent": True, "paid": False},
        {"taxYear": "2024", "due": 0, "delinquent": True, "paid": True},           # paid late, settled
        {"taxYear": "2023", "due": 1110.50, "delinquent": True, "paid": False},
    ]
    got = te._parse_delinquency(bills)
    assert got["delinquent_amount"] == 2310.5 and got["delinquent_years"] == 2
    assert got["delinquent_year_list"] == ["2023", "2025"]


def test_the_lien_note_no_longer_quotes_the_raw_amount_due(tmp_path):
    import json
    (tmp_path / "knox_lien_parcels.json").write_text(json.dumps([{
        "name": "DOE PAT", "n_parcels": 1, "n_liens": 1, "types": ["LEN"],
        "instruments": ["202609030000020"], "creditors": ["ACME HOA"],
        "filed_last": "2026-09-03", "tax_unpaid_years": [2026],
        "parcels": [{"account": "010AA-001", "owner": "DOE PAT",
                     "address": "100 EXAMPLE ST", "amount_due": 1840.22}],
    }]), encoding="utf-8")
    (tmp_path / "lien_releases.json").write_text(
        json.dumps({"released_instruments": [], "released_names": []}), encoding="utf-8")
    (tmp_path / "lien_amounts.json").write_text(
        json.dumps({"202609030000020": 2500.0}), encoding="utf-8")

    recs = kp.load_liens(str(tmp_path))

    assert len(recs) == 1 and recs[0]["tax_account"] == "010AA-001"
    assert "amount due" not in recs[0]["notes"] and "Unpaid county tax" not in recs[0]["notes"]
    assert recs[0]["lien_total"] == 2500.0


# ── one row per property ─────────────────────────────────────────────


def _lien(address, total, debtor="DOE PAT"):
    return {"source": "register_of_deeds_lien", "address": address, "lien_total": total,
            "notes": "Lien debtor %s" % debtor}


def test_co_debtors_at_one_house_become_one_row_with_the_larger_lien():
    rows = [_lien("100 EXAMPLE ST", 500.0, "DOE PAT"),
            {"source": "condemnation", "address": "100 EXAMPLE ST", "notes": "agenda"},
            _lien("100  Example St", 2500.0, "DOE SAM"),
            _lien("7 OTHER RD", 80.0)]

    out = kp.one_lien_row_per_address(rows)

    assert [r["source"] for r in out] == ["register_of_deeds_lien", "condemnation", "register_of_deeds_lien"]
    assert out[0]["lien_total"] == 2500.0 and "DOE SAM" in out[0]["notes"]
    assert out[2]["address"] == "7 OTHER RD"


def test_the_county_search_returning_a_parcel_twice_yields_one_parcel(monkeypatch):
    import io
    import json as _json

    import knox_lien_resolve as klr

    payload = {"parcels": [
        {"account_number": "010AA-001", "owner": "DOE PAT", "parcel_address": "100 EXAMPLE ST", "amount_due": 900},
        {"account_number": "010AA-001", "owner": "DOE PAT", "parcel_address": "100 EXAMPLE ST", "amount_due": 1800},
        {"account_number": "010AA-002", "owner": "DOE PAT & SAM", "parcel_address": "102 EXAMPLE ST", "amount_due": 0},
        {"account_number": "099ZZ-009", "owner": "DOE PATRICIA ANN", "parcel_address": "9 ELSEWHERE LN", "amount_due": 0},
    ], "bills": []}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(klr.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(_json.dumps(payload).encode()))

    rec = klr.resolve({"name": "DOE PAT"})

    assert rec["n_parcels"] == 2 and not rec["institutional"]
    assert [p["account"] for p in rec["parcels"]] == ["010AA-001", "010AA-002"]
