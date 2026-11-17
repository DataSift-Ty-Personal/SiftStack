"""The release and amount lookups behind the Knox lien list. Zero network.

Every name and instrument number here is invented.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import knox_lien_index as kli  # noqa: E402


def _row(doc, instrument, name="DOE", first="PAT", role="Grantor", other="ACME BANK",
         marginal="", consideration=""):
    r = [""] * 16
    r[3] = "<table><tr><td>Instrument: %s</td></tr></table>" % instrument
    r[4], r[5], r[6], r[7], r[8] = name, first, role, other, doc
    r[10], r[13], r[14] = instrument, marginal, consideration
    return r


def _write(folder, code, rows):
    with open(os.path.join(folder, "rod2_%s.json" % code), "w", encoding="utf-8") as fh:
        json.dump({"aaData": rows}, fh)


def test_a_release_names_the_lien_it_clears_not_itself():
    rows = [_row("RSTL", "202609010000001", marginal="202403050000042 (STL)")]
    released, names = kli.released_from_rows(rows)
    assert released == {"202403050000042"}
    assert names == {"DOE PAT"}


def test_one_release_can_clear_several_liens():
    rows = [_row("REL", "202609010000002",
                 marginal="<a>202401010000007</a> (LEN), 202402020000008 (LEN)")]
    released, _ = kli.released_from_rows(rows)
    assert released == {"202401010000007", "202402020000008"}


def test_a_map_reference_in_the_marginal_is_not_an_instrument():
    # Lien rows put a tax map reference there ("MAP 61S 31").
    released, _ = kli.released_from_rows([_row("REL", "202609010000003", marginal="MAP 61S 31")])
    assert released == set()


def test_amounts_come_from_consideration_and_blank_stays_blank():
    rows = [_row("LEN", "202609020000010", consideration="$1,155.00"),
            _row("STL", "202609020000011", consideration=""),
            _row("LEN", "202609020000012", consideration="$0.00")]
    assert kli.amounts_from_rows(rows) == {"202609020000010": 1155.0}


def test_build_stands_a_gap_pull_on_earlier_history(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "lien_releases.json").write_text(json.dumps(
        {"released_instruments": ["202501010000001"], "released_names": ["ROE SAM"]}), encoding="utf-8")
    (old / "lien_amounts.json").write_text(json.dumps({"202501010000001": 900.0}), encoding="utf-8")
    _write(str(new), "LEN", [_row("LEN", "202609030000020", consideration="$2,500.00")])
    _write(str(new), "REL", [_row("REL", "202610010000030", marginal="202609030000020 (LEN)")])

    stats = kli.build(str(new), [str(old)])

    rel = json.loads((new / "lien_releases.json").read_text(encoding="utf-8"))
    amt = json.loads((new / "lien_amounts.json").read_text(encoding="utf-8"))
    assert set(rel["released_instruments"]) == {"202501010000001", "202609030000020"}
    assert "ROE SAM" in rel["released_names"] and "DOE PAT" in rel["released_names"]
    assert amt == {"202501010000001": 900.0, "202609030000020": 2500.0}
    assert stats["released_added"] == 1 and stats["amounts_added"] == 1


def test_build_is_idempotent(tmp_path):
    _write(str(tmp_path), "LEN", [_row("LEN", "202609030000040", consideration="$75.50")])
    _write(str(tmp_path), "RESL", [_row("RESL", "202610010000041", marginal="202609030000040")])
    kli.build(str(tmp_path))
    again = kli.build(str(tmp_path))
    assert again["released_added"] == 0 and again["amounts_added"] == 0


def test_short_or_malformed_rows_are_ignored(tmp_path):
    with open(os.path.join(str(tmp_path), "rod2_REL.json"), "w", encoding="utf-8") as fh:
        json.dump({"aaData": [["too", "short"], "not a row", None]}, fh)
    assert kli.build(str(tmp_path))["released"] == 0
