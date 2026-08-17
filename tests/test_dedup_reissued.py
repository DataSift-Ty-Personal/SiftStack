"""One property advertised repeatedly must collapse to one row.

A continued or republished foreclosure sale is re-advertised weekly and gets a
NEW notice id each time. Deduping by notice id alone therefore let every one
through: a staged month produced 134 rows for 88 distinct properties, 46 rows of
pure repetition. DataSift upserts by address, so those extra rows do not create
duplicate CRM records, but the same property gets written several times and
whichever row lands last wins.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from data_formatter import deduplicate  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

BASE = "https://www.tnpublicnotice.com/(S(x))/Details.aspx?SID=x&ID="


def _n(nid: str, addr: str, published: str, ntype: str = "foreclosure",
       city: str = "Knoxville", owner: str = "") -> NoticeData:
    return NoticeData(
        notice_type=ntype, county="Knox", address=addr, city=city,
        owner_name=owner, date_published=published, source_url=BASE + nid,
    )


class TestReissuedNotices:
    def test_same_property_two_notice_ids_collapses(self):
        out = deduplicate([
            _n("472732", "5713 Gaboury Ln", "2025-09-05"),
            _n("474407", "5713 Gaboury Ln", "2025-09-26"),
        ])
        assert len(out) == 1

    def test_most_recent_publication_wins(self):
        out = deduplicate([
            _n("472732", "5713 Gaboury Ln", "2025-09-05", owner="old"),
            _n("474407", "5713 Gaboury Ln", "2025-09-26", owner="new"),
        ])
        assert out[0].owner_name == "new"

    def test_order_does_not_matter(self):
        newest_first = deduplicate([
            _n("474407", "5713 Gaboury Ln", "2025-09-26", owner="new"),
            _n("472732", "5713 Gaboury Ln", "2025-09-05", owner="old"),
        ])
        assert len(newest_first) == 1
        assert newest_first[0].owner_name == "new"

    def test_case_and_whitespace_insensitive(self):
        out = deduplicate([
            _n("1", "5713 Gaboury Ln", "2025-09-05"),
            _n("2", "  5713 GABOURY LN ", "2025-09-26"),
        ])
        assert len(out) == 1


class TestThingsThatMustNotCollapse:
    def test_different_properties_both_kept(self):
        out = deduplicate([
            _n("1", "5713 Gaboury Ln", "2025-09-05"),
            _n("2", "2131 Bishops Bridge Rd", "2025-09-05"),
        ])
        assert len(out) == 2

    def test_probate_and_foreclosure_at_one_address_both_kept(self):
        """Two real leads on two different lists. Collapsing drops one."""
        out = deduplicate([
            _n("1", "100 Main St", "2025-09-05", ntype="foreclosure"),
            _n("2", "100 Main St", "2025-09-06", ntype="probate"),
        ])
        assert len(out) == 2
        assert {n.notice_type for n in out} == {"foreclosure", "probate"}

    def test_same_street_different_city_kept(self):
        out = deduplicate([
            _n("1", "100 Main St", "2025-09-05", city="Knoxville"),
            _n("2", "100 Main St", "2025-09-05", city="Maryville"),
        ])
        assert len(out) == 2

    def test_address_less_records_are_never_merged(self):
        """Probate before lookup has no address; merging would fuse estates."""
        out = deduplicate([
            _n("1", "", "2025-09-05", ntype="probate"),
            _n("2", "", "2025-09-06", ntype="probate"),
        ])
        assert len(out) == 2

    def test_identical_notice_id_still_collapses(self):
        out = deduplicate([
            _n("999", "1 A St", "2025-09-05"),
            _n("999", "1 A St", "2025-09-05"),
        ])
        assert len(out) == 1


class TestOrdering:
    def test_input_order_is_preserved(self):
        out = deduplicate([
            _n("1", "A St", "2025-09-01"),
            _n("2", "B St", "2025-09-02"),
            _n("3", "C St", "2025-09-03"),
        ])
        assert [n.address for n in out] == ["A St", "B St", "C St"]
