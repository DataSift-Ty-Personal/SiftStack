"""End-to-end wiring tests for the name-variant resolver -> PVA path.

Spec task 2e-5 / NAME-01 / NAME-02 acceptance: a decedent whose property is
titled under a MAIDEN name resolves end-to-end through the PVA variant loop
(the canonical Jackson -> GREATHOUSE 0-row case), and without maiden context
the lookup attaches NOTHING rather than a wrong parcel.

Network-free: the PVA per-query primitive ``search_by_owner`` and the
session/login/detail helpers are replaced with in-process stubs, so no HTTP
is made. Standalone-script style per TESTING.md (no pytest; bare asserts +
print PASS; ``__main__`` runner).

Run:  python tests/test_name_resolver_wiring.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import kentucky_pva_lookup as pva  # noqa: E402
from kentucky_pva_lookup import PvaRow, probate_property_lookup  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


# ── Stub seam ─────────────────────────────────────────────────────────
# probate_property_lookup() flow per call:
#   _make_session() -> _login() -> for each candidate: _lookup_one() -> _logout()
# _lookup_one() loops generate_variants(...) values into search_by_owner(),
# scores rows against the VARIANT value, then on a hit calls get_detail() and
# _apply_to_notice() which writes notice.address / notice.parcel_id.
#
# We stub the network seam only: search_by_owner returns a parcel ONLY for a
# query containing "GREATHOUSE" (the maiden surname), [] otherwise. Session
# helpers are no-ops; get_detail returns {} so _apply_to_notice falls back to
# row.address / row.parcel_id (no detail-page HTTP).

_GREATHOUSE_ROW = PvaRow(
    address="2120 Hale Ave",
    owner="GREATHOUSE DOROTHY E",
    parcel_id="ABC123456789",
    lrsn="999999",
    legal="LOT 7 PARK HILL",
)


def _fake_search_by_owner(session, owner_name, max_pages=None):
    """Return the GREATHOUSE parcel only when the query is the maiden form.

    Mirrors the live primitive's signature; ``[]`` for any non-maiden query
    (the JACKSON variants), which is exactly the 0-rows-under-JACKSON case.
    """
    if "GREATHOUSE" in (owner_name or "").upper():
        return [_GREATHOUSE_ROW]
    return []


def _install_stubs():
    """Replace the network seam with in-process no-ops/fakes. Returns a
    restore() callable so the module is left clean for other tests."""
    orig = {
        "search_by_owner": pva.search_by_owner,
        "_make_session": pva._make_session,
        "_login": pva._login,
        "_logout": pva._logout,
        "get_detail": pva.get_detail,
    }
    pva.search_by_owner = _fake_search_by_owner
    pva._make_session = lambda: object()        # no real requests.Session
    pva._login = lambda session: True           # pretend auth succeeded
    pva._logout = lambda session: None          # no-op
    pva.get_detail = lambda session, lrsn: {}   # no detail-page HTTP

    def restore():
        for k, v in orig.items():
            setattr(pva, k, v)

    return restore


# ── Tests ─────────────────────────────────────────────────────────────
def test_maiden_titled_resolves_via_pva():
    """Jackson -> GREATHOUSE: maiden context resolves the parcel end-to-end."""
    restore = _install_stubs()
    try:
        notice = NoticeData(
            notice_type="probate",
            county="Jefferson",
            state="KY",
            decedent_name="Dorothy Emma Jackson",
        )
        # Maiden context as the obituary step (Plan 03) would inject it; the
        # PVA wiring reads it via getattr(notice, "decedent_obit_maiden_name").
        notice.decedent_obit_maiden_name = "Greathouse"

        probate_property_lookup([notice])

        assert notice.address, (
            "maiden-titled lookup did not populate address "
            f"(got address={notice.address!r}, parcel={notice.parcel_id!r})"
        )
        assert notice.parcel_id == "ABC123456789", (
            f"expected GREATHOUSE parcel attached, got {notice.parcel_id!r}"
        )
        # Confirm it resolved via the maiden parcel specifically.
        assert "Hale Ave" in notice.address, f"Got: {notice.address!r}"
    finally:
        restore()
    print("PASS: test_maiden_titled_resolves_via_pva (Jackson -> GREATHOUSE)")


def test_no_maiden_does_not_false_attach():
    """No maiden context: all JACKSON variants return 0 rows -> no attach."""
    restore = _install_stubs()
    try:
        notice = NoticeData(
            notice_type="probate",
            county="Jefferson",
            state="KY",
            decedent_name="Dorothy Emma Jackson",
        )
        # No maiden / aka context at all — the obituary step never ran.

        probate_property_lookup([notice])

        assert not notice.address, (
            f"no-maiden lookup wrongly attached address {notice.address!r}"
        )
        assert not notice.parcel_id, (
            f"no-maiden lookup wrongly attached parcel {notice.parcel_id!r}"
        )
        assert not notice.estimated_value, (
            f"no-maiden lookup wrongly attached value {notice.estimated_value!r}"
        )
    finally:
        restore()
    print("PASS: test_no_maiden_does_not_false_attach (no wrong-parcel attach)")


if __name__ == "__main__":
    test_maiden_titled_resolves_via_pva()
    test_no_maiden_does_not_false_attach()
    print("\nAll name_resolver wiring tests passed (network-free PVA path).")
