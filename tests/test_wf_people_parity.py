"""enformion_business keeps exact copies of three helpers so the principal finder and the buyer
registry load on the agents brain, which has no browser: norm_name and ORG_RX (enterprise_prospects)
and person_first_last (enformion_ftm.clean_owner_name, whose module imports Playwright). These checks
pin each copy to its original, so a fix made in one place and not the other fails here.

Zero network. Invented names.

Run: python tests/test_wf_people_parity.py   (or pytest)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import enformion_business as eb  # noqa: E402
import enterprise_prospects as ep  # noqa: E402

NAMES = ["Invented Holdings, L.L.C.", "o'brien  pat", "L P Example Partners", "SAMPLE   VENTURES llc", "",
         "Pat & Jo Example", "Casey Q. Tester Jr.", "Robin Sample AKA Rob Sample", "MORALES FAMILY TRUST",
         "E Bourgeois", "Lee Counsel, Esq.", "Example Pat c/o Example Jo", "D'Angelo-Smith Mary"]


def test_norm_name_is_the_same_function():
    for n in NAMES:
        assert eb.norm_name(n) == ep.norm_name(n), n


def test_org_rx_is_the_same_pattern():
    assert eb.ORG_RX.pattern == ep.ORG_RX.pattern
    assert eb.ORG_RX.flags == ep.ORG_RX.flags


def test_person_first_last_matches_clean_owner_name():
    try:
        from enformion_ftm import clean_owner_name
    except ImportError as exc:          # a box without Playwright cannot load the original at all
        print("skipped: enformion_ftm does not load here (%s)" % exc)
        return
    for n in NAMES:
        assert eb.person_first_last(n) == clean_owner_name(n), n


def test_dispo_buyers_reads_the_same_copies():
    import dispo_buyers as D
    assert D._is_human is eb._is_human and D.rank_officers is eb.rank_officers
    assert D.principal_confidence is eb.principal_confidence and D.split_mail is eb.split_mail
    assert D.person_first_last is eb.person_first_last


def test_the_modules_hold_no_long_dash_or_backspace_byte():
    for name in ("enformion_business.py", "buyer_sweep.py", "dispo_buyers.py"):
        text = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text, name


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok  " + name)
    print("%d checks passed" % n)
