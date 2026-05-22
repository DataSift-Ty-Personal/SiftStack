"""Network-free unit tests for the deep-prospect Level-1 skip-trace wiring (2g-1).

Covers the deep_prospector._run_level_1 fix (CONTACT-01):
  * a deceased-owner notice with NO phones gets phones populated via a (mocked)
    batch_skip_trace, then run through the death/identity guard;
  * the full canonical DM_PHONE_FIELDS set is counted (no 6-field undercount —
    a notice that only has mobile_4/mobile_5/landline_3 is treated as "has phones"
    and is NOT re-traced);
  * --no-skip-trace (skip_trace=False) suppresses the trace;
  * a missing TRACERFY_API_KEY suppresses the trace;
  * the "Would call tracerfy here in production" stub string is gone from source.

No network: batch_skip_trace + guard_traced_contacts are injected via the
deep_prospector module attributes per test. Standalone-script style per
TESTING.md (bare asserts + print PASS).

Run:  python tests/test_deep_prospect_trace.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config  # noqa: E402
import deep_prospector as dp  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def test_deceased_no_phones_gets_traced():
    """A deceased-owner notice with no phones -> batch_skip_trace is called and
    populates phones; the guard runs; result reports phones found via tracerfy."""
    saved_bst = getattr(dp, "batch_skip_trace", None)
    saved_guard = getattr(dp, "guard_traced_contacts", None)
    saved_key = config.TRACERFY_API_KEY
    calls = {"trace": 0, "guard": 0}

    def fake_batch(notices, *a, **k):
        calls["trace"] += 1
        for n in notices:
            n.primary_phone = "5025551234"
            n.mobile_1 = "5025555678"
        return {"matched": 1, "phones_found": 2, "emails_found": 0}

    def fake_guard(notice):
        calls["guard"] += 1
        return {"suppressed_phones": 0, "suppressed_emails": 0,
                "unconfirmed": False, "notes": ""}

    config.TRACERFY_API_KEY = "TEST_KEY"
    # Inject the seams the implementation must use.
    dp.batch_skip_trace = fake_batch
    dp.guard_traced_contacts = fake_guard
    try:
        n = NoticeData()
        n.owner_deceased = "yes"
        n.decision_maker_name = "Jane Heir"
        result = dp.ProspectResult(address="1 Main St", owner_name="Decedent")
        _run(dp._run_level_1(n, result, skip_trace=True))

        assert calls["trace"] == 1, f"batch_skip_trace not called once: {calls}"
        assert calls["guard"] == 1, f"guard not called once: {calls}"
        assert n.primary_phone == "5025551234", n.primary_phone
        assert result.phones_found == 2, result.phones_found
        assert result.skip_trace_provider == "tracerfy", result.skip_trace_provider
        assert result.depth_completed == 1, result.depth_completed
    finally:
        config.TRACERFY_API_KEY = saved_key
        if saved_bst is not None:
            dp.batch_skip_trace = saved_bst
        elif hasattr(dp, "batch_skip_trace"):
            del dp.batch_skip_trace
        if saved_guard is not None:
            dp.guard_traced_contacts = saved_guard
        elif hasattr(dp, "guard_traced_contacts"):
            del dp.guard_traced_contacts
    print("PASS: test_deceased_no_phones_gets_traced")


def test_full_dm_phone_field_count_no_undercount():
    """A notice whose ONLY phone is in mobile_4 / mobile_5 / landline_3 (the three
    fields the old 6-field count missed) must be treated as already-having-phones
    and NOT re-traced (no wasted Tracerfy credit)."""
    saved_bst = getattr(dp, "batch_skip_trace", None)
    saved_key = config.TRACERFY_API_KEY
    calls = {"trace": 0}

    def boom(notices, *a, **k):
        calls["trace"] += 1
        raise AssertionError("batch_skip_trace should NOT run — record already has a phone")

    config.TRACERFY_API_KEY = "TEST_KEY"
    dp.batch_skip_trace = boom
    try:
        for fld in ("mobile_4", "mobile_5", "landline_3"):
            n = NoticeData()
            setattr(n, fld, "5025550000")
            result = dp.ProspectResult()
            _run(dp._run_level_1(n, result, skip_trace=True))
            assert result.phones_found >= 1, f"{fld}: undercounted -> {result.phones_found}"
            assert result.skip_trace_provider == "existing", \
                f"{fld}: re-traced instead of using existing -> {result.skip_trace_provider}"
        assert calls["trace"] == 0, f"batch_skip_trace wrongly called: {calls}"
    finally:
        config.TRACERFY_API_KEY = saved_key
        if saved_bst is not None:
            dp.batch_skip_trace = saved_bst
        elif hasattr(dp, "batch_skip_trace"):
            del dp.batch_skip_trace
    print("PASS: test_full_dm_phone_field_count_no_undercount")


def test_no_skip_trace_flag_suppresses_trace():
    """skip_trace=False (--no-skip-trace) -> batch_skip_trace is never called even
    when the record has no phones and a key is configured."""
    saved_bst = getattr(dp, "batch_skip_trace", None)
    saved_key = config.TRACERFY_API_KEY

    def boom(notices, *a, **k):
        raise AssertionError("batch_skip_trace should NOT run when skip_trace=False")

    config.TRACERFY_API_KEY = "TEST_KEY"
    dp.batch_skip_trace = boom
    try:
        n = NoticeData()
        n.owner_deceased = "yes"
        result = dp.ProspectResult()
        _run(dp._run_level_1(n, result, skip_trace=False))
        assert result.phones_found == 0, result.phones_found
        assert "suppress" in result.notes.lower() or "no-skip-trace" in result.notes.lower(), result.notes
    finally:
        config.TRACERFY_API_KEY = saved_key
        if saved_bst is not None:
            dp.batch_skip_trace = saved_bst
        elif hasattr(dp, "batch_skip_trace"):
            del dp.batch_skip_trace
    print("PASS: test_no_skip_trace_flag_suppresses_trace")


def test_missing_api_key_no_trace():
    """Empty TRACERFY_API_KEY -> the trace is skipped gracefully, no crash."""
    saved_bst = getattr(dp, "batch_skip_trace", None)
    saved_key = config.TRACERFY_API_KEY

    def boom(notices, *a, **k):
        raise AssertionError("batch_skip_trace should NOT run with no API key")

    config.TRACERFY_API_KEY = ""
    dp.batch_skip_trace = boom
    try:
        n = NoticeData()
        n.owner_deceased = "yes"
        result = dp.ProspectResult()
        _run(dp._run_level_1(n, result, skip_trace=True))
        assert result.phones_found == 0, result.phones_found
        assert "configured" in result.notes.lower() or "no phones" in result.notes.lower(), result.notes
    finally:
        config.TRACERFY_API_KEY = saved_key
        if saved_bst is not None:
            dp.batch_skip_trace = saved_bst
        elif hasattr(dp, "batch_skip_trace"):
            del dp.batch_skip_trace
    print("PASS: test_missing_api_key_no_trace")


def test_threaded_through_prospect_record():
    """prospect_record(notice, skip_trace=False) threads the flag to level 1 ->
    no trace runs even on a phone-less deceased record."""
    saved_bst = getattr(dp, "batch_skip_trace", None)
    saved_key = config.TRACERFY_API_KEY

    def boom(notices, *a, **k):
        raise AssertionError("trace ran despite prospect_record(skip_trace=False)")

    config.TRACERFY_API_KEY = "TEST_KEY"
    dp.batch_skip_trace = boom
    try:
        n = NoticeData()
        n.owner_deceased = "yes"
        n.address = "5 Oak St"
        result = _run(dp.prospect_record(n, target_depth=1, skip_trace=False))
        assert result.depth_completed == 1, result.depth_completed
        assert result.phones_found == 0, result.phones_found
    finally:
        config.TRACERFY_API_KEY = saved_key
        if saved_bst is not None:
            dp.batch_skip_trace = saved_bst
        elif hasattr(dp, "batch_skip_trace"):
            del dp.batch_skip_trace
    print("PASS: test_threaded_through_prospect_record")


def test_stub_string_gone():
    """The "Would call tracerfy here in production" stub comment is removed."""
    src_path = os.path.join(os.path.dirname(__file__), "..", "src", "deep_prospector.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert "Would call tracerfy here in production" not in src, "stub string still present"
    assert "DM_PHONE_FIELDS" in src, "DM_PHONE_FIELDS not used (still 6-field undercount?)"
    assert "batch_skip_trace" in src, "batch_skip_trace not wired"
    print("PASS: test_stub_string_gone")


if __name__ == "__main__":
    test_deceased_no_phones_gets_traced()
    test_full_dm_phone_field_count_no_undercount()
    test_no_skip_trace_flag_suppresses_trace()
    test_missing_api_key_no_trace()
    test_threaded_through_prospect_record()
    test_stub_string_gone()
    print("\nALL PASS: deep_prospect_trace")
