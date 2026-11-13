"""The daily Knox lien refresh. Zero network, invented data.

The recorder pull and the county name search are both injected, so these
tests exercise only the bookkeeping: what counts as NEW, and when an
instrument may be marked handled.
"""
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_liens_daily as kd  # noqa: E402


def _row(doc, instrument, name, first, role="Grantor", marginal="", consideration="", recorded="9/3/2026"):
    r = [""] * 16
    r[3] = "Instrument: %s Recorded: %s 10:00:00 AM" % (instrument, recorded)
    r[4], r[5], r[6], r[7], r[8] = name, first, role, "ACME HOA", doc
    r[10], r[13], r[14] = instrument, marginal, consideration
    return r


def _puller(rows_by_type, problems=None):
    def pull(scratch, months):
        for doc, rows in rows_by_type.items():
            with open(os.path.join(scratch, "rod2_%s.json" % doc), "w", encoding="utf-8") as fh:
                json.dump({"aaData": rows}, fh)
        return {"rows": {k: len(v) for k, v in rows_by_type.items()},
                "problems": list(problems or []), "window": "09/01/2026 .. 09/30/2026",
                "subscription_days": 21}
    return pull


def _resolver(parcels_by_name):
    def resolve(debtors):
        out = []
        for d in debtors:
            hits = parcels_by_name.get(d["name"], [])
            out.append(dict(d, n_parcels=len(hits), parcels=hits, institutional=False,
                            tax_unpaid_years=[]))
        return out
    return resolve


def _load(tmp_path, name):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


LIENS = {
    "LEN": [_row("LEN", "202609030000020", "DOE", "PAT", consideration="$2,500.00"),
            _row("LEN", "202609040000021", "ROE", "SAM", consideration="$900.00")],
    "REL": [_row("REL", "202609200000030", "ACME HOA", "", role="Grantee",
                 marginal="202609040000021 (LEN)")],
}
PARCELS = {"DOE PAT": [{"account": "010AA-001", "owner": "DOE PAT",
                        "address": "100 EXAMPLE ST", "amount_due": 0}]}


def test_first_run_stages_every_debtor_and_builds_the_lookups(tmp_path):
    stats = kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))

    assert stats["new_debtors"] == 2 and stats["with_parcels"] == 1
    assert stats["subscription_days"] == 21
    staged = _load(tmp_path, "knox_lien_parcels.json")
    assert {d["name"] for d in staged} == {"DOE PAT", "ROE SAM"}
    assert _load(tmp_path, "lien_pending.json") == ["202609030000020", "202609040000021"]
    assert "202609040000021" in _load(tmp_path, "lien_releases.json")["released_instruments"]
    assert _load(tmp_path, "lien_amounts.json")["202609030000020"] == 2500.0


def test_nothing_is_marked_handled_until_the_upload_succeeds(tmp_path):
    kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))
    assert not (tmp_path / "lien_uploaded.json").exists()

    # The upload failed, so tomorrow's run stages the same two debtors again.
    again = kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))
    assert again["new_debtors"] == 2


def test_after_a_successful_upload_the_same_liens_are_not_sent_twice(tmp_path):
    kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))
    assert kd.mark_uploaded(str(tmp_path)) == 2
    assert _load(tmp_path, "knox_lien_parcels.json") == []

    again = kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))

    assert again["new_debtors"] == 0 and again["with_parcels"] == 0
    assert _load(tmp_path, "knox_lien_parcels.json") == []


def test_an_unmatched_debtor_is_marked_handled_too(tmp_path):
    # ROE SAM never matches a parcel. Re-trying daily costs 15 seconds a name.
    kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))
    kd.mark_uploaded(str(tmp_path))
    assert "202609040000021" in _load(tmp_path, "lien_uploaded.json")


def test_a_new_lien_on_a_known_debtor_stages_that_debtor_again(tmp_path):
    kd.prepare(str(tmp_path), puller=_puller(LIENS), resolver=_resolver(PARCELS))
    kd.mark_uploaded(str(tmp_path))
    more = {"LEN": LIENS["LEN"] + [_row("LEN", "202609250000040", "DOE", "PAT",
                                        consideration="$700.00")], "REL": LIENS["REL"]}

    stats = kd.prepare(str(tmp_path), puller=_puller(more), resolver=_resolver(PARCELS))

    assert stats["new_debtors"] == 1
    staged = _load(tmp_path, "knox_lien_parcels.json")
    assert [d["name"] for d in staged] == ["DOE PAT"] and staged[0]["n_liens"] == 2


def test_an_incomplete_pull_stops_the_stage_instead_of_staging_half_a_list(tmp_path):
    with pytest.raises(RuntimeError, match="incomplete"):
        kd.prepare(str(tmp_path), puller=_puller(LIENS, problems=["REL 09/01/2026: Timeout: x"]),
                   resolver=_resolver(PARCELS))
    assert not (tmp_path / "knox_lien_parcels.json").exists()


def test_mark_uploaded_with_nothing_pending_is_a_no_op(tmp_path):
    assert kd.mark_uploaded(str(tmp_path)) == 0
    assert not (tmp_path / "lien_uploaded.json").exists()


def test_the_previous_month_is_pulled_too_in_the_first_days_of_a_month():
    assert kd.months_to_pull(date(2026, 12, 2)) == 2
    assert kd.months_to_pull(date(2026, 12, 18)) == 1


def test_a_ninth_lien_is_not_hidden_behind_the_eight_kept_for_the_note(tmp_path):
    nine = [_row("LEN", "2026090300000%02d" % n, "DOE", "PAT", consideration="$100.00")
            for n in range(1, 10)]
    kd.prepare(str(tmp_path), puller=_puller({"LEN": nine[:8]}), resolver=_resolver(PARCELS))
    kd.mark_uploaded(str(tmp_path))

    stats = kd.prepare(str(tmp_path), puller=_puller({"LEN": nine}), resolver=_resolver(PARCELS))

    assert stats["new_debtors"] == 1
    assert "202609030000009" in _load(tmp_path, "lien_pending.json")
