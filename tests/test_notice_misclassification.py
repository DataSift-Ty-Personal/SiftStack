"""Foreclosure notices must not leak into the probate pipeline.

The probate saved search matches on the word "probate", which also appears in
trustee sale notices. A successor-trustee sale therefore surfaces in the probate
results, and without a guard it uploads to the Probate list with the trustee's
law firm parsed as the personal representative. Seen live 2026-08-14 on a
Marinosci Law Group notice, which produced "PR = From Felicia F. Coalson".
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from foreclosure_filter import is_valid_foreclosure, looks_like_trustee_sale  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

TRUSTEE_SALE_IN_PROBATE_RESULTS = """SUBSTITUTE TRUSTEE'S SALE
Sale at public auction will be on September 12, 2026, at the Knox County
Courthouse. This sale is subject to all matters shown on any applicable
recorded plat. Marinosci Law Group, P.C., Successor Trustee
555 Perkins Extended, Suite 445 Memphis, TN 38117
Office: (901) 203-0680 Fax: (901) 440-0561
"""

REAL_PROBATE = """NOTICE TO CREDITORS

ESTATE OF DORIS O. YOUNG
Notice is hereby given that Letters Testamentary in respect to the Estate of
DORIS O. YOUNG, who died on April 13, 2026, were issued to the referenced
Personal Representative by the Chancery Court, Probate Division.

PERSONAL REPRESENTATIVE(S)
ERIC LEE SHARP
"""

# A probate filing that references a trust deed. Must NOT be reclassified.
PROBATE_MENTIONING_TRUSTEE = """NOTICE TO CREDITORS
ESTATE OF JAMES REED
Letters of Administration were issued to the Personal Representative. The
decedent's property remains subject to a deed of trust held by a trustee.
Claims against the estate must be filed with the clerk.
"""


def _n(notice_type: str, raw: str) -> NoticeData:
    return NoticeData(notice_type=notice_type, county="Knox", raw_text=raw)


class TestTrusteeSaleDetection:
    def test_trustee_sale_body_is_detected(self):
        assert looks_like_trustee_sale(_n("probate", TRUSTEE_SALE_IN_PROBATE_RESULTS))

    def test_real_probate_is_not_a_trustee_sale(self):
        assert not looks_like_trustee_sale(_n("probate", REAL_PROBATE))

    def test_probate_mentioning_a_trustee_is_not_a_sale(self):
        """A probate anchor beats a passing mention of a trustee."""
        assert not looks_like_trustee_sale(_n("probate", PROBATE_MENTIONING_TRUSTEE))

    def test_empty_body_is_not_a_sale(self):
        assert not looks_like_trustee_sale(_n("probate", ""))


class TestFiltering:
    def test_trustee_sale_is_dropped_from_probate_results(self):
        assert not is_valid_foreclosure(_n("probate", TRUSTEE_SALE_IN_PROBATE_RESULTS))

    def test_real_probate_still_passes(self):
        assert is_valid_foreclosure(_n("probate", REAL_PROBATE))

    def test_probate_mentioning_trustee_still_passes(self):
        assert is_valid_foreclosure(_n("probate", PROBATE_MENTIONING_TRUSTEE))

    def test_foreclosure_typed_notices_unaffected(self):
        """The normal foreclosure path must behave exactly as before."""
        assert is_valid_foreclosure(_n("foreclosure", TRUSTEE_SALE_IN_PROBATE_RESULTS))
        assert not is_valid_foreclosure(_n("foreclosure", REAL_PROBATE))

    def test_address_less_probate_without_body_passes(self):
        """Probate records legitimately reach the filter with a thin body."""
        assert is_valid_foreclosure(_n("probate", ""))
