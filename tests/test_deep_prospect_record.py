"""The single-record deep prospect (src/deep_prospect_record.py): what it adds on top of the shared
write-back rules. Zero network; every name, street and number is invented.

    .venv/Scripts/python.exe -m pytest tests/test_deep_prospect_record.py -q -p no:cacheprovider
"""
import contextlib
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import deep_prospect_record as dpr  # noqa: E402
import dream100_dp as dp  # noqa: E402
from agents import selftest_writeback as st  # noqa: E402
from agents.research import writeback as wb  # noqa: E402

PATCHED = ("top_rows", "_tax", "smartskip_rows", "assemble", "writeback_plan", "pack_md", "build_note")


@pytest.fixture()
def one_record(tmp_path, monkeypatch):
    """A run folder for one record with dream100_dp pointed at it, the way the CLI does it, and
    put back afterwards: patch() swaps module functions, which would leak into every later test."""
    for key in wb.SETTING_NAMES:
        monkeypatch.delenv(key, raising=False)
    for name in PATCHED:
        monkeypatch.setattr(dp, name, getattr(dp, name))
    monkeypatch.setattr(dp, "READBACK_PAUSE_S", 0)
    monkeypatch.setattr(dp, "render_pdf", lambda md, pdf, photo=None: pdf)
    run = st._write_run(tmp_path, "one")
    row = dict(run.load("records.json")[0]["row"], uuid=st.U, owner_uuid=st.OW, parcel_id="", tax_query="")
    dpr.patch(row)
    return run, row


def reach(name, tag, role):
    return {"name": name, "relationship": tag.lower(), "tag": tag, "role": role, "owner_like": False}


def test_a_shared_line_carries_the_decision_makers_tie_and_the_tier_rule_still_holds(one_record):
    shared = st.row("8655550131", "Dial First", [reach("Odessa Brackwater", "Aunt", "channel"),
                                                reach("Pell Quillfeather", "Son", "signer")])
    third = st.row("8655550132", "Dial Third", [reach("Pell Quillfeather", "Son", "signer")])
    plan = dpr.writeback_plan({"x": {"owner_uuid": st.OW}, "dial": [shared, third]})
    assert [p["number"] for p in plan["phones"]] == ["8655550131"]
    assert plan["phones"][0]["tags"] == ["Son", "Dial First", "SmartSkip"]          # the signer, not the first by alphabet
    assert plan["held"][0]["number"] == "8655550132" and "reserve" in plan["held"][0]["why"]


def test_a_third_tier_number_is_written_when_it_is_all_the_record_has(one_record):
    third = st.row("8655550132", "Dial Third", [reach("Pell Quillfeather", "Son", "signer")])
    plan = dpr.writeback_plan({"x": {"owner_uuid": st.OW}, "dial": [third]})
    assert [p["number"] for p in plan["phones"]] == ["8655550132"] and plan["held"] == []


def test_the_build_leaves_each_heirs_share_and_clears_it_when_one_person_signs(one_record):
    run, row = one_record
    with contextlib.redirect_stdout(io.StringIO()):
        dpr.phase_build(SimpleNamespace(commit=False), run, row)
    shares = run.load("heir_shares.json")
    assert shares["computed"] and [h["fraction"] for h in shares["heirs"]] == ["1/3", "1/3", "1/3"]
    assert shares["heirs"][0]["name"] == "Marla Quillfeather" and shares["heirs"][0]["verified"] is True
    one = st.analysis(people=st.analysis()["people"][:1], must_sign=st.analysis()["must_sign"][:1])
    run.save("analysis/%s.json" % st.SLUG, one)
    with contextlib.redirect_stdout(io.StringIO()):
        dpr.phase_build(SimpleNamespace(commit=False), run, row)
    assert run.load("heir_shares.json") is None          # the earlier file does not outlive its research


def test_the_note_a_caller_reads_carries_who_must_sign_and_no_long_dash(one_record):
    run, _ = one_record
    note = dp.build_note(dp.assemble(run)[0])
    assert "Who must sign:" in note and "Marla Quillfeather" in note
    assert chr(8212) not in note and chr(8211) not in note
    assert st.N_MARLA not in note and st.N_MARLA[-4:] in note      # last four digits only


def test_the_module_holds_no_long_dash_or_backspace_byte():
    text = (SRC / "deep_prospect_record.py").read_text(encoding="utf-8")
    assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
