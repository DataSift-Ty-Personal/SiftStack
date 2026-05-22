"""Standalone, network-free tests for the re-poll queue store (Phase 6 / COVER-01).

Run: .venv/Scripts/python.exe tests/test_repoll_queue.py

Mirrors the repo's standalone-test convention (bare functions + assert + print PASS,
no pytest, no network). The real state file is NEVER touched — config.KCOJ_REPOLL_FILE
is monkeypatched to a throwaway tempfile before the store is exercised.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, _SRC)

import config  # noqa: E402

# ── Redirect the queue's state file to a throwaway tempfile (network-free,
#    never touches the real kcoj_repoll_queue.json). Do this BEFORE importing
#    the module so its `from config import KCOJ_REPOLL_FILE` reads the patched
#    value is irrelevant — the module always calls config.KCOJ_REPOLL_FILE via
#    config.* at call time, so patching config here is sufficient.
config.KCOJ_REPOLL_FILE = Path(tempfile.mkdtemp(prefix="repoll_test_")) / "q.json"

import kcoj_repoll_queue as q  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _fresh_file():
    """Point the store at a brand-new empty tempfile for an isolated round-trip."""
    config.KCOJ_REPOLL_FILE = Path(tempfile.mkdtemp(prefix="repoll_test_")) / "q.json"


def test_enqueue_persist_reload():
    _fresh_file()
    queue = q.load_repoll_queue()
    assert queue == {}, f"expected empty queue, got {queue}"
    q.enqueue_repoll(queue, "26-P-009999", reason="courtnet_0_rows", today="2026-05-18")
    assert "26-P-009999" in queue
    entry = queue["26-P-009999"]
    assert entry["attempts"] == 0, f"new entry attempts should be 0, got {entry}"
    assert entry["repoll_after"] > "2026-05-18", f"repoll_after should be future, got {entry}"
    assert entry["reason"] == "courtnet_0_rows"
    q.save_repoll_queue(queue)
    reloaded = q.load_repoll_queue()
    assert reloaded == queue, f"round-trip mismatch: {reloaded} != {queue}"
    assert reloaded["26-P-009999"]["attempts"] == 0
    print("PASS: test_enqueue_persist_reload")


def test_enqueue_does_not_reset_existing():
    _fresh_file()
    queue = {}
    q.enqueue_repoll(queue, "26-P-000001", reason="first", today="2026-05-18")
    # Simulate a prior bump having raised attempts.
    queue["26-P-000001"]["attempts"] = 2
    original_date = queue["26-P-000001"]["repoll_after"]
    q.enqueue_repoll(queue, "26-P-000001", reason="second", today="2026-05-25")
    assert queue["26-P-000001"]["attempts"] == 2, "re-enqueue must NOT reset attempts"
    assert queue["26-P-000001"]["repoll_after"] == original_date, "re-enqueue must NOT change date"
    print("PASS: test_enqueue_does_not_reset_existing")


def test_due_filtering():
    _fresh_file()
    queue = {
        "PAST": {"repoll_after": "2026-05-01", "attempts": 0, "reason": ""},
        "TODAY": {"repoll_after": "2026-05-22", "attempts": 0, "reason": ""},
        "FUTURE": {"repoll_after": "2026-12-31", "attempts": 0, "reason": ""},
    }
    due = q.due_entries(queue, today="2026-05-22")
    assert "PAST" in due, "past-dated entry must be due"
    assert "TODAY" in due, "same-day entry (<=) must be due"
    assert "FUTURE" not in due, "future-dated entry must NOT be due"
    assert len(due) == 2, f"expected 2 due, got {due}"
    print("PASS: test_due_filtering")


def test_business_days_skips_weekend():
    # 2026-05-18 is a Monday; +4 business days -> Fri 2026-05-22.
    got = q.business_days_from("2026-05-18", 4)
    assert got == "2026-05-22", f"Mon+4bd expected 2026-05-22, got {got}"
    # 2026-05-21 is a Thursday; +4 business days -> Wed 2026-05-27
    # (Fri, [skip Sat/Sun], Mon, Tue, Wed).
    got2 = q.business_days_from("2026-05-21", 4)
    assert got2 == "2026-05-27", f"Thu+4bd expected 2026-05-27, got {got2}"
    print("PASS: test_business_days_skips_weekend")


def test_max_attempts_drop():
    _fresh_file()
    queue = {}
    q.enqueue_repoll(queue, "26-P-000777", today="2026-05-18")  # attempts=0
    # bump 1: 0 -> 1 (bumped), bump 2: 1 -> 2 (bumped), bump 3: 2 -> 3 == max -> dropped
    r1 = q.bump_or_drop(queue, "26-P-000777", today="2026-05-18", max_attempts=3)
    assert r1 == "bumped", f"first bump should bump, got {r1}"
    assert queue["26-P-000777"]["attempts"] == 1
    r2 = q.bump_or_drop(queue, "26-P-000777", today="2026-05-18", max_attempts=3)
    assert r2 == "bumped", f"second bump should bump, got {r2}"
    assert queue["26-P-000777"]["attempts"] == 2
    r3 = q.bump_or_drop(queue, "26-P-000777", today="2026-05-18", max_attempts=3)
    assert r3 == "dropped", f"third bump should drop at max, got {r3}"
    assert "26-P-000777" not in queue, "exhausted entry must be removed from queue"
    print("PASS: test_max_attempts_drop")


def test_make_key():
    # case_number wins when present.
    n1 = NoticeData(case_number="26-P-001234", decedent_name="SMITH JOHN")
    assert q.make_key(n1) == "26-P-001234", f"case_number should win, got {q.make_key(n1)}"
    # falls back to DECEDENT|date when no case_number.
    n2 = NoticeData(case_number="", decedent_name="DOE JANE")
    setattr(n2, "filing_date", "2026-05-13")
    key2 = q.make_key(n2)
    assert key2 == "DOE JANE|2026-05-13", f"expected DOE JANE|2026-05-13, got {key2}"
    # empty decedent AND empty case -> "".
    n3 = NoticeData(case_number="", decedent_name="")
    assert q.make_key(n3) == "", f"empty decedent+case should be '', got {q.make_key(n3)!r}"
    print("PASS: test_make_key")


def test_no_repoll_after_NOT_redefined_by_phase6():
    """Cross-phase invariant: Phase 6 must NOT REDEFINE repoll_after (Phase 5 owns it).

    This is robust to Phase 5 shipping: 0 declarations (Phase 5 not yet merged) AND
    1 declaration (Phase 5 merged) both pass. It also tolerates Phase 5's legitimate
    forward-reference comment ("drained by Phase 6") on its own declaration line —
    that is a CONSUMER reference, not a Phase-6 OWNERSHIP claim. The signal that would
    indicate Phase 6 wrongly claimed the field is a Phase-6 ownership marker
    (COVER-01/COVER-02) on the declaration line, or a second declaration.
    """
    parser_path = os.path.join(_SRC, "notice_parser.py")
    lines = [
        l for l in open(parser_path, encoding="utf-8").read().splitlines()
        if re.match(r"\s*repoll_after\s*:", l)
    ]
    assert len(lines) <= 1, (
        f"Phase 6 must not redefine/duplicate repoll_after; found {len(lines)} declarations"
    )
    # No declaration line may carry a Phase-6 OWNERSHIP marker (COVER-01/COVER-02).
    # Phase 5's "drained by Phase 6 (2g-6)" consumer comment does NOT carry these.
    for l in lines:
        assert "COVER-01" not in l and "COVER-02" not in l, (
            f"Phase 6 ownership marker on repoll_after declaration: {l.strip()!r}"
        )
    # Phase 6's own two fields must each appear exactly once.
    src = open(parser_path, encoding="utf-8").read().splitlines()
    attempts = [l for l in src if re.match(r"\s*repoll_attempts\s*:", l)]
    heir = [l for l in src if re.match(r"\s*heir_id_source\s*:", l)]
    assert len(attempts) == 1, f"expected exactly 1 repoll_attempts decl, got {len(attempts)}"
    assert len(heir) == 1, f"expected exactly 1 heir_id_source decl, got {len(heir)}"
    print("PASS: test_no_repoll_after_NOT_redefined_by_phase6")


if __name__ == "__main__":
    test_enqueue_persist_reload()
    test_enqueue_does_not_reset_existing()
    test_due_filtering()
    test_business_days_skips_weekend()
    test_max_attempts_drop()
    test_make_key()
    test_no_repoll_after_NOT_redefined_by_phase6()
    print("\nALL PASS: test_repoll_queue")
