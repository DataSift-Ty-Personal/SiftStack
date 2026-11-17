"""A released lien must come back off the DataSift record. Zero network, invented data.

The DataSift client is a fake that stores custom fields, tags and notes per
record, so the read-back in `apply_change` is exercised for real.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_lien_releases as kr  # noqa: E402

AMOUNTS = {"L1": 2500.0, "L2": 900.0, "L3": 40000.0}
FIELDS = ["Outstanding Lien Amount", "Liens Active vs Total", "Total Delinquency",
          "Debt vs Equity Pct", "Tax delinquency amount", "Est Equity"]


class FakeApi:
    """Just enough of apiv2 for the release path."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def call(self, path, method="GET", body=None, headers=None):
        self.calls.append((method, path))
        uuid = path.split("/property/")[1].split("/")[0] if "/property/" in path else ""
        if path.startswith("/api/internal/custom-fields/"):
            return {"results": [{"label": f, "uuid": "f-" + f, "field_type": "text", "options": []}
                                for f in FIELDS]}
        rec = self.records[uuid]
        if path.endswith("/custom-field/?limit=1000"):
            return {"results": [{"custom_field": {"label": k}, "value": v} for k, v in rec["fields"].items()]}
        if path.endswith("/custom-field/update-values/"):
            for pair in body:
                rec["fields"][pair["field_uuid"][2:]] = pair["value"]
            return {}
        if path.endswith("/add-notes/"):
            rec["notes"] += " | " + body["notes"]
            return {}
        if path.endswith("/remove-tags/"):
            rec["tags"] = [t for t in rec["tags"] if t not in body["tags"]]
            return {}
        if path.endswith("/remove-lists/"):
            # The live route takes a STRING. An array returns 201 and does nothing.
            if isinstance(body["lists"], str):
                rec["lists"] = [t for t in rec.get("lists", ["Liens"]) if t != body["lists"]]
            return {}
        if method == "PATCH":
            # The live route only ADDS: a tag left out of the list survives.
            rec["tags"] += [t for t in body["tags"] if t not in rec["tags"]]
            return {}
        return {"notes": rec["notes"], "tags": [{"title": t} for t in rec["tags"]],
                "lists": [{"title": t} for t in rec.setdefault("lists", ["Liens"])]}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(kr, "CRM_GAP", 0)
    monkeypatch.setattr(kr.time, "sleep", lambda *_: None)


def _scratch(tmp_path, ledger, released):
    (tmp_path / kr.LEDGER).write_text(json.dumps(ledger), encoding="utf-8")
    (tmp_path / "lien_releases.json").write_text(
        json.dumps({"released_instruments": sorted(released), "released_names": []}), encoding="utf-8")
    (tmp_path / "lien_amounts.json").write_text(json.dumps(AMOUNTS), encoding="utf-8")
    return str(tmp_path)


def _ledger(shown_active=2, shown_amount=3400.0):
    return {"u1": {"street": "100 Example St", "zip": "37900",
                   "debtors": {"DOE PAT": ["L1", "L2"]},
                   "shown": {"active": shown_active, "total": 2, "amount": shown_amount}}}


def _record(amount="3400.0", tags=("Courthouse Data", "lien", "lien_under_5k"), tax=None):
    fields = {"Outstanding Lien Amount": amount, "Liens Active vs Total": "2 of 2",
              "Total Delinquency": amount, "Est Equity": "200000"}
    if tax is not None:
        fields["Tax delinquency amount"] = tax
    return {"notes": "Lien debtor DOE PAT: 2 lien instrument(s) recorded (LEN).",
            "tags": list(tags), "fields": fields}


def test_nothing_changes_while_every_lien_is_still_open(tmp_path):
    stats = kr.reconcile(_scratch(tmp_path, _ledger(), released=set()), commit=False)
    assert stats["to_update"] == 0


def test_a_dry_run_counts_but_writes_nothing(tmp_path):
    api = FakeApi({"u1": _record()})
    stats = kr.reconcile(_scratch(tmp_path, _ledger(), {"L1", "L2"}), api=api, commit=False)
    assert stats["to_update"] == 1 and stats["fully_released"] == 1 and stats["updated"] == 0
    assert api.calls == []


def test_a_fully_released_record_goes_to_zero_and_is_tagged(tmp_path):
    api = FakeApi({"u1": _record()})
    scratch = _scratch(tmp_path, _ledger(), {"L1", "L2"})

    stats = kr.reconcile(scratch, api=api, commit=True)

    rec = api.records["u1"]
    assert stats["updated"] == 1 and stats["mismatch"] == 0
    assert float(rec["fields"]["Outstanding Lien Amount"]) == 0.0
    assert rec["fields"]["Liens Active vs Total"] == "0 of 2"
    assert float(rec["fields"]["Total Delinquency"]) == 0.0
    assert kr.RELEASED_TAG in rec["tags"] and "lien_under_5k" not in rec["tags"]
    assert "Courthouse Data" in rec["tags"] and "lien" in rec["tags"]
    assert "2 lien(s) released" in rec["notes"] and "0 of 2" in rec["notes"]


def test_a_partial_release_drops_the_total_to_what_is_still_owed(tmp_path):
    api = FakeApi({"u1": _record()})
    kr.reconcile(_scratch(tmp_path, _ledger(), {"L1"}), api=api, commit=True)

    rec = api.records["u1"]
    assert float(rec["fields"]["Outstanding Lien Amount"]) == 900.0
    assert rec["fields"]["Liens Active vs Total"] == "1 of 2"
    assert kr.RELEASED_TAG not in rec["tags"] and "lien_under_5k" in rec["tags"]


def test_tax_already_on_the_record_stays_in_total_delinquency(tmp_path):
    api = FakeApi({"u1": _record(tax="1742.07")})
    kr.reconcile(_scratch(tmp_path, _ledger(), {"L1", "L2"}), api=api, commit=True)
    assert float(api.records["u1"]["fields"]["Total Delinquency"]) == 1742.07


def test_the_same_release_is_not_written_twice(tmp_path):
    api = FakeApi({"u1": _record()})
    scratch = _scratch(tmp_path, _ledger(), {"L1", "L2"})
    kr.reconcile(scratch, api=api, commit=True)
    writes = len(api.calls)

    again = kr.reconcile(scratch, api=api, commit=True)

    assert again["to_update"] == 0 and len(api.calls) == writes


def test_a_record_that_does_not_name_the_debtor_is_never_rewritten(tmp_path):
    stranger = _record()
    stranger["notes"] = "Lien debtor ROE SAM: 1 lien instrument(s) recorded (LEN)."
    api = FakeApi({"u1": stranger})

    stats = kr.reconcile(_scratch(tmp_path, _ledger(), {"L1", "L2"}), api=api, commit=True)

    assert stats["skipped"] == 1 and stats["updated"] == 0
    assert api.records["u1"]["fields"]["Outstanding Lien Amount"] == "3400.0"
    assert not any(m in ("PATCH", "POST") for m, _ in api.calls)


def test_a_record_restated_from_one_months_filings_is_put_back_to_the_full_picture(tmp_path):
    # August said 2 of 2. A later upload saw only the new lien and wrote "1 of 1".
    ledger = {"u1": {"street": "100 Example St", "zip": "37900",
                     "debtors": {"DOE PAT": ["L1", "L2", "L3"]},
                     "shown": {"active": 1, "total": 1, "amount": 40000.0}}}
    api = FakeApi({"u1": _record(amount="40000.0", tags=("lien", "lien_15k_50k"))})

    kr.reconcile(_scratch(tmp_path, ledger, released=set()), api=api, commit=True)

    rec = api.records["u1"]
    assert float(rec["fields"]["Outstanding Lien Amount"]) == 43400.0
    assert rec["fields"]["Liens Active vs Total"] == "3 of 3"
    assert "Lien figures restated" in rec["notes"] and "lien_15k_50k" in rec["tags"]


def test_two_debtors_on_one_house_are_counted_together_without_doubling(tmp_path):
    ledger = {}
    kr.add_debtor(ledger, "u1", "100 Example St", "37900", "DOE PAT", ["L1", "L2"])
    entry = kr.add_debtor(ledger, "u1", "100 Example St", "37900", "DOE SAM", ["L2", "L3"])
    now = kr.truth(entry, released={"L3"}, amounts=AMOUNTS)
    assert now["total"] == 3 and now["active"] == 2 and now["amount"] == 3400.0


def test_debtor_names_are_read_out_of_the_notes():
    notes = ("No delinquent county tax. Lien debtor DOE PAT: 2 lien instrument(s) recorded (LEN). "
             "| Lien debtor ROE-SMITH SAM A: 1 lien instrument(s) recorded (FTL).")
    assert kr.debtors_from_notes(notes) == ["DOE PAT", "ROE-SMITH SAM A"]
    assert kr.debtors_from_notes("") == [] and kr.debtors_from_notes(None) == []


def test_the_band_tag_follows_the_amount():
    assert kr.band(626.6) == "lien_under_5k" and kr.band(5000) == "lien_5k_15k"
    assert kr.band(151655) == "lien_over_100k"


def test_a_record_that_already_reads_true_is_left_untouched(tmp_path):
    # The ledger believed "2 of 2"; the record had already been corrected.
    rec = _record(amount="0")
    rec["fields"]["Liens Active vs Total"] = "0 of 2"
    api = FakeApi({"u1": rec})
    scratch = _scratch(tmp_path, _ledger(), {"L1", "L2"})

    stats = kr.reconcile(scratch, api=api, commit=True)

    assert stats["already_true"] == 1 and stats["updated"] == 0
    # No figure, tag or note is rewritten. It reads zero active liens, though,
    # so it still comes off the Liens list.
    assert not any(m == "PATCH" or p.endswith(("/update-values/", "/add-notes/", "/remove-tags/"))
                   for m, p in api.calls)
    assert api.records["u1"]["lists"] == [] and stats["unlisted"] == 1
    assert kr.reconcile(scratch, api=api, commit=True)["to_update"] == 0


def test_a_pile_up_of_debtor_names_is_never_summed(tmp_path):
    ledger = {"u1": {"street": "100 Example St", "zip": "37900",
                     "debtors": {"DOE A": ["L1"], "DOE B": ["L2"], "DOE C": ["L3"], "DOE D": ["L1"]},
                     "shown": {"active": 1, "total": 1, "amount": 2500.0}}}
    assert kr.plan(ledger, released={"L1"}, amounts=AMOUNTS) == []


def test_the_old_amount_band_tag_is_removed_not_left_beside_the_new_one(tmp_path):
    ledger = {"u1": {"street": "100 Example St", "zip": "37900",
                     "debtors": {"DOE PAT": ["L1", "L3"]},
                     "shown": {"active": 2, "total": 2, "amount": 42500.0}}}
    rec = _record(amount="42500.0", tags=("lien", "lien_15k_50k"))
    rec["fields"]["Liens Active vs Total"] = "2 of 2"
    api = FakeApi({"u1": rec})

    kr.reconcile(_scratch(tmp_path, ledger, {"L3"}), api=api, commit=True)

    tags = api.records["u1"]["tags"]
    assert "lien_under_5k" in tags and "lien_15k_50k" not in tags and "lien" in tags
    assert ("POST", "/api/internal/property/u1/remove-tags/") in api.calls


def test_a_lien_recorded_after_a_full_release_takes_the_released_tag_back_off(tmp_path):
    ledger = {"u1": {"street": "100 Example St", "zip": "37900",
                     "debtors": {"DOE PAT": ["L1", "L2", "L3"]},
                     "shown": {"active": 0, "total": 2, "amount": 0.0}}}
    rec = _record(amount="0", tags=("lien", kr.RELEASED_TAG))
    rec["fields"]["Liens Active vs Total"] = "0 of 2"
    api = FakeApi({"u1": rec})

    kr.reconcile(_scratch(tmp_path, ledger, {"L1", "L2"}), api=api, commit=True)

    tags = api.records["u1"]["tags"]
    assert kr.RELEASED_TAG not in tags and "lien_15k_50k" in tags
    assert api.records["u1"]["fields"]["Liens Active vs Total"] == "1 of 3"


# ── the daily hand-off: what was just uploaded goes into the ledger ───


def _upload_csv(tmp_path, rows):
    import csv as _csv
    path = tmp_path / "knox_ftm_pull_test.csv"
    cols = ["Property Street Address", "Property ZIP Code", "Lists", "Notes",
            "Liens Active vs Total", "Outstanding Lien Amount"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return str(path)


def test_an_uploaded_lien_row_is_remembered_with_every_instrument(tmp_path, monkeypatch):
    import datasift_api_upload as up
    monkeypatch.setattr(up, "find_property", lambda api, addr: "u9")
    (tmp_path / "knox_lien_parcels.json").write_text(json.dumps([
        {"name": "DOE PAT", "n_parcels": 1, "institutional": False,
         "instruments": ["L1"], "all_instruments": ["L1", "L2"],
         "parcels": [{"address": "100 EXAMPLE ST", "account": "010AA-001"}]},
        {"name": "DOE SAM", "n_parcels": 1, "institutional": False,
         "instruments": ["L3"], "all_instruments": ["L3"],
         "parcels": [{"address": "100 EXAMPLE STREET", "account": "010AA-001"}]},
        {"name": "ROE LEE", "n_parcels": 1, "institutional": False,
         "instruments": ["L9"], "all_instruments": ["L9"],
         "parcels": [{"address": "7 OTHER RD", "account": "020BB-002"}]},
    ]), encoding="utf-8")
    path = _upload_csv(tmp_path, [
        {"Property Street Address": "100 Example St", "Property ZIP Code": "37900", "Lists": "Liens",
         "Notes": "Filed 2026-09-03 | No delinquent county tax. Lien debtor DOE PAT: 2 lien instrument(s) recorded (LEN).",
         "Liens Active vs Total": "2 of 2", "Outstanding Lien Amount": "3400.0"},
        {"Property Street Address": "55 Agenda Ave", "Property ZIP Code": "37900", "Lists": "Condemned",
         "Notes": "agenda", "Liens Active vs Total": "", "Outstanding Lien Amount": ""},
    ])

    added = kr.register_uploaded(str(tmp_path), path, api=object())

    ledger = kr.load_ledger(str(tmp_path))
    assert added == 1 and list(ledger) == ["u9"]
    # The co-debtor at the same house is remembered; the stranger down the road is not.
    assert ledger["u9"]["debtors"] == {"DOE PAT": ["L1", "L2"], "DOE SAM": ["L3"]}
    assert ledger["u9"]["shown"] == {"active": 2, "total": 2, "amount": 3400.0}
    # ...so the very next comparison restates the house to all three liens.
    plan = kr.plan(ledger, released=set(), amounts=AMOUNTS)
    assert plan[0]["after"] == {"active": 3, "total": 3, "amount": 43400.0}


def test_a_row_whose_record_cannot_be_found_is_not_put_in_the_ledger(tmp_path, monkeypatch):
    import datasift_api_upload as up
    monkeypatch.setattr(up, "find_property", lambda api, addr: "")
    (tmp_path / "knox_lien_parcels.json").write_text(json.dumps([
        {"name": "DOE PAT", "n_parcels": 1, "institutional": False, "instruments": ["L1"],
         "parcels": [{"address": "100 EXAMPLE ST"}]}]), encoding="utf-8")
    path = _upload_csv(tmp_path, [
        {"Property Street Address": "100 Example St", "Property ZIP Code": "37900", "Lists": "Liens",
         "Notes": "Lien debtor DOE PAT: 1 lien instrument(s) recorded (LEN).",
         "Liens Active vs Total": "1 of 1", "Outstanding Lien Amount": "2500"}])

    assert kr.register_uploaded(str(tmp_path), path, api=object()) == 0
    assert kr.load_ledger(str(tmp_path)) == {}


# ── off the lead list once every lien is released (Ty, 2026-11-11) ───


def test_a_fully_released_record_comes_off_the_liens_list_and_no_other(tmp_path):
    rec = _record()
    rec["lists"] = ["High Equity", "Liens", "Probate"]
    api = FakeApi({"u1": rec})

    stats = kr.reconcile(_scratch(tmp_path, _ledger(), {"L1", "L2"}), api=api, commit=True)

    assert stats["updated"] == 1 and stats["unlisted"] == 1
    assert api.records["u1"]["lists"] == ["High Equity", "Probate"]
    assert json.loads((tmp_path / kr.UNLISTED).read_text(encoding="utf-8")) == ["u1"]


def test_a_partly_released_record_stays_on_the_list(tmp_path):
    api = FakeApi({"u1": _record()})
    stats = kr.reconcile(_scratch(tmp_path, _ledger(), {"L1"}), api=api, commit=True)
    assert stats["unlisted"] == 0 and api.records["u1"]["lists"] == ["Liens"]


def test_a_record_that_still_shows_an_open_lien_is_not_taken_off_the_list(tmp_path):
    # The ledger says released, but the record was never zeroed (a failed write).
    ledger = _ledger(shown_active=0, shown_amount=0.0)
    api = FakeApi({"u1": _record()})          # still reads "2 of 2"

    stats = kr.unlist_released(_scratch(tmp_path, ledger, {"L1", "L2"}), api=api, commit=True)

    assert stats["not_ready"] == 1 and stats["unlisted"] == 0
    assert api.records["u1"].get("lists", ["Liens"]) == ["Liens"]
    # Not recorded as done, so the next run looks at it again.
    done = tmp_path / kr.UNLISTED
    assert not done.exists() or "u1" not in json.loads(done.read_text(encoding="utf-8"))


def test_a_dry_unlist_only_counts(tmp_path):
    api = FakeApi({"u1": _record()})
    stats = kr.unlist_released(_scratch(tmp_path, _ledger(0, 0.0), {"L1", "L2"}), api=api, commit=False)
    assert stats["to_unlist"] == 1 and api.calls == []


def test_a_new_lien_after_a_full_release_lets_the_record_be_unlisted_again_later(tmp_path, monkeypatch):
    import datasift_api_upload as up
    monkeypatch.setattr(up, "find_property", lambda api, addr: "u1")
    scratch = _scratch(tmp_path, _ledger(0, 0.0), {"L1", "L2"})
    (tmp_path / kr.UNLISTED).write_text(json.dumps(["u1", "u7"]), encoding="utf-8")
    (tmp_path / "knox_lien_parcels.json").write_text(json.dumps([
        {"name": "DOE PAT", "n_parcels": 1, "institutional": False, "instruments": ["L3"],
         "all_instruments": ["L3"], "parcels": [{"address": "100 EXAMPLE ST"}]}]), encoding="utf-8")
    path = _upload_csv(tmp_path, [
        {"Property Street Address": "100 Example St", "Property ZIP Code": "37900", "Lists": "Liens",
         "Notes": "Lien debtor DOE PAT: 1 lien instrument(s) recorded (LEN).",
         "Liens Active vs Total": "1 of 1", "Outstanding Lien Amount": "40000"}])

    kr.register_uploaded(scratch, path, api=object())

    assert json.loads((tmp_path / kr.UNLISTED).read_text(encoding="utf-8")) == ["u7"]
    after = kr.plan(kr.load_ledger(scratch), {"L1", "L2"}, AMOUNTS)[0]["after"]
    assert after == {"active": 1, "total": 3, "amount": 40000.0}
