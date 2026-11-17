"""What a deep prospect reads first and what it may write on a record
(src/agents/research/writeback.py and the chain around it). Zero network: a fake DataSift held in
memory, every model and vendor step replaced by a recorder. Every name here is invented.

The checks themselves live in src/agents/selftest_writeback.py, so the agents selftest and this
file run the same ones; here each group is its own test.

    .venv/Scripts/python.exe -m pytest tests/test_writeback.py -q -p no:cacheprovider
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import config  # noqa: E402
from agents import selftest_writeback as st  # noqa: E402
from agents.research import open_research as o  # noqa: E402
from agents.research import writeback as wb  # noqa: E402


class Checks:
    def __init__(self):
        self.passed, self.failed = [], []

    def __call__(self, ok, msg):
        (self.passed if ok else self.failed).append(msg)

    def done(self, at_least: int):
        assert not self.failed, "\n".join(self.failed)
        assert len(self.passed) >= at_least, "only %d checks ran" % len(self.passed)


@pytest.fixture()
def sealed(tmp_path, monkeypatch):
    """A throwaway database and run folder, the box dry, and every write-back setting cleared, so
    nothing this machine's own .env carries can change a result."""
    for name, value in (("DB_PATH", tmp_path / "agents.db"), ("RUNS_DIR", tmp_path / "runs"), ("MODE", "dry"),
                        ("RUN_CAP_USD", 5.0), ("DAILY_CAP_USD", 25.0), ("RESEARCH_DAILY_CAP_USD", 25.0),
                        ("FINAL_RESERVE_USD", 1.0)):
        monkeypatch.setattr(config, name, value)
    for key in wb.SETTING_NAMES:
        monkeypatch.delenv(key, raising=False)
    from agents import store
    store.init()
    return tmp_path


def test_the_rules_that_decide_what_is_written(sealed):
    check = Checks()
    st._rules(check)
    check.done(90)


def test_each_heirs_share(sealed):
    check = Checks()
    st._shares(check)
    check.done(32)


def test_the_free_look_before_any_paid_trace(sealed):
    check = Checks()
    st._free_look(check, sealed)
    check.done(16)


def test_each_new_write_against_a_fake_datasift(sealed):
    check = Checks()
    st._api_helpers(check, sealed)
    check.done(19)


def test_the_write_back_run_twice(sealed):
    check = Checks()
    st._chain(check, sealed)
    check.done(41)


def test_the_order_of_the_chain(sealed):
    from agents import store
    check = Checks()
    st._order(check, sealed, store)
    check.done(15)


def test_the_whole_section_the_way_the_agents_selftest_runs_it(sealed):
    check = Checks()
    st.run(check, sealed)
    check.done(213)


NAMES = [("Quillfeather, Marla J", "Marla Quillfeather"), ("Marla Jean Quillfeather", "Marla Quillfeather Sr."),
         ("Marla Quillfeather", "Dorn Quillfeather"), ("", "Marla Quillfeather"),
         ("Dorn Quillfeather Sr", "Dorn Quillfeather Jr"), ("Dorn Quillfeather", "Dorn Quillfeather Jr"),
         ("Dorn Maurice Quillfeather", "Pell Maurice Quillfeather"), ("Pell Dorn Quillfeather", "Dorn Maurice Quillfeather"),
         ("Ines Nicole Quillfeather Brackwater", "Ines Quillfeather"), ("Susan Theresa Brackwater", "Theresa Brackwater"),
         ("QUILLFEATHER DORN M", "Dorn Quillfeather"), ("Marla", "Marla"), ("Marla", "Marla Quillfeather"),
         ("Dorn W. Quillfeather", "Dorn Quillfeather (owner)"), ("O'Neal Brackwater", "Oneal Brackwater")]


def test_the_name_test_here_is_the_research_engines_own():
    # writeback keeps its own copy so it imports nothing but the standard library; this pins the two together
    for a, b in NAMES:
        for strict in (False, True):
            assert wb.same_name(a, b, strict) == o.same_person(a, b, strict), (a, b, strict)
            assert wb.same_name(b, a, strict) == o.same_person(b, a, strict), (b, a, strict)


def test_writeback_imports_nothing_but_the_standard_library():
    # dream100_dp imports it on a workstation that may not carry the model clients
    text = (SRC / "agents" / "research" / "writeback.py").read_text(encoding="utf-8")
    top = [ln for ln in text.splitlines() if ln.startswith(("import ", "from "))]
    assert sorted(top) == ["from __future__ import annotations", "from fractions import Fraction", "from pathlib import Path",
                           "import os", "import re"]


def test_the_new_modules_hold_no_long_dash_or_backspace_byte():
    for rel in ("agents/research/writeback.py", "agents/selftest_writeback.py", "agents/research/researcher.py",
                "agents/research/deep_prospect.py", "dream100_dp.py", "deep_prospect_record.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text, rel
