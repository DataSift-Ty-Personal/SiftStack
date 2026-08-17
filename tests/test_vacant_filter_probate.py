"""The vacant-land filter must not delete address-less notice types.

A mixed foreclosure + probate run used to lose every probate record here: the
filter judges by house number, probate notices carry no property address at that
point in the pipeline, and the foreclosures came through fine so the run looked
healthy while the whole type vanished.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from enrichment_pipeline import NO_ADDRESS_TYPES, _filter_vacant_land  # noqa: E402
from notice_parser import NoticeData  # noqa: E402


def _n(notice_type: str, address: str = "") -> NoticeData:
    return NoticeData(notice_type=notice_type, county="Knox", address=address)


class TestVacantLandFilter:
    def test_probate_without_address_survives(self):
        out = _filter_vacant_land([_n("probate")])
        assert len(out) == 1

    def test_divorce_without_address_survives(self):
        out = _filter_vacant_land([_n("divorce")])
        assert len(out) == 1

    def test_foreclosure_without_house_number_still_removed(self):
        """The filter must keep doing its real job on types that carry an address."""
        out = _filter_vacant_land([_n("foreclosure", "0 Andersonville Pike")])
        assert out == []

    def test_foreclosure_with_no_address_removed(self):
        assert _filter_vacant_land([_n("foreclosure", "")]) == []

    def test_foreclosure_with_house_number_kept(self):
        out = _filter_vacant_land([_n("foreclosure", "158 Old State Rd")])
        assert len(out) == 1

    def test_mixed_run_keeps_probate_and_drops_vacant_foreclosure(self):
        """The exact shape of a scheduled Knox+Blount run."""
        batch = [
            _n("foreclosure", "158 Old State Rd"),
            _n("foreclosure", "0 Andersonville Pike"),
            _n("probate"),
            _n("probate"),
        ]
        out = _filter_vacant_land(batch)
        kinds = sorted(n.notice_type for n in out)
        assert kinds == ["foreclosure", "probate", "probate"], kinds

    def test_probate_is_declared_address_less(self):
        assert "probate" in NO_ADDRESS_TYPES

    def test_probate_resolved_to_vacant_land_is_dropped(self):
        """The exemption covers "not looked up yet", not "exempt forever".
        A probate lookup that resolves to "0 Minnis Ave" found a real vacant
        lot; seven of those reached DataSift before this was distinguished."""
        assert _filter_vacant_land([_n("probate", "0 Minnis Ave")]) == []
        assert _filter_vacant_land([_n("probate", "0000 Shipe Rd")]) == []

    def test_probate_with_a_real_address_still_kept(self):
        out = _filter_vacant_land([_n("probate", "9406 Island Creek Ln")])
        assert len(out) == 1
