"""Probate notice parsing, regression tests from live Knox notices.

Every fixture below is the real body text of a notice scraped from
tnpublicnotice.com on 2026-08-14, trimmed only where marked. Each one encodes a
bug that shipped records into DataSift:

  * "Notice to Creditors" names the role in prose before naming the human, so a
    same-line pattern set owner_name to "By The Chancery Court".
  * "Notice of Service by Publication" states the COURTHOUSE address, which was
    parsed as the subject property.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

from notice_parser import NoticeData, _parse_address, _parse_name, _parse_pr_address  # noqa: E402


# Knox, ID 555859, published 2026-08-03. Middle paragraphs of statutory
# boilerplate trimmed; the prose mention of the role is kept because it is the
# thing that used to win.
NOTICE_TO_CREDITORS = """NOTICE TO CREDITORS

ESTATE OF DORIS O. YOUNG
DOCKET NUMBER 92868-2
Notice is hereby given that on the 27th day of July, 2026, Letters Testamentary in respect to the Estate of DORIS O. YOUNG, who died on April 13, 2026, were issued to the referenced Personal Representative by the Chancery Court, Probate Division, of Knox County, Tennessee. All persons, resident and non-resident, having claims, matured or unmatured, against the estate are required to file the same with the clerk of the above-named court on or before the earlier of the dates prescribed in (1) or (2), otherwise their claims will be forever barred:
(2) Twelve (12) months from the decedent's date of death.
This the 27th day of JULY, 2026.

ESTATE OF DORIS O. YOUNG

PERSONAL REPRESENTATIVE(S)
ERIC LEE SHARP
806 SUNNYDALE ROAD
KNOXVILLE, TN 37923

DONALD J. FARINATO, ATTORNEY
445 S. GAY STREET, SUITE 401
KNOXVILLE, TN 37902
"""

# Knox, ID 555771, published 2026-08-10. Names the PR before the role.
SERVICE_BY_PUBLICATION_INRE = """NOTICE OF SERVICE BY PUBLICATION

TO: CHRISTOPHER GLENN, SUMMER GREENLEE, KENDALL GLENN, and ANY UNKNOWN HEIRS of
LUCY ANN GLENN (deceased)

IN RE: BRENT BAXLEY, Personal Representative
Vs.
CHRISTOPHER GLENN, SUMMER GREENLEE, KENDALL GLENN, and ANY UNKNOWN HEIRS of LUCY ANN GLENN (deceased), Defendants

No. 78185-3
IN THE CHANCERY COURT FOR KNOX COUNTY, TENNESSEE, PROBATE DIVISION
Therefore, it is ordered that said Defendants file an answer with the Clerk & Master of the Chancery Court (Probate Division) at Knoxville, Tennessee and with L. Eric Ebbert, Petitioner's attorney, whose address is The Ebbert Law Firm, 9145 Cross Park Dr, Suite 103, Knoxville, TN 37923.

Hon. Christopher D. Heagerty
Chancellor
"""

# Knox, ID 555821, published 2026-08-03. States the courthouse address.
SERVICE_BY_PUBLICATION_BY = """NOTICE OF SERVICE BY PUBLICATION

TO: LISA BULLOCK

IN RE: THE ESATE OF JACK EUGENE MCMAHAN

BY: LEE ROY MCMAHAN, Personal Representative / Petitioner
Vs.
LISA BULLOCK, Respondent

No. 90335-1

IN THE CHANCERY COURT FOR KNOX COUNTY, TENNESSEE,
PROBATE DIVISION
Therefore, it is ORDERED that LISA BULLOCK, Respondent, appear before the Chancery Court for Knox County, Tennessee, Probate Division, located at 400 W. Main Street, Suite 125, Knoxville, Tennessee 37902, on the 17th day of September, 2026, at 9:30 a.m., to respond to the Petition.

Honorable John F. Weaver
Chancellor
"""


def _parse(raw: str) -> NoticeData:
    n = NoticeData(notice_type="probate", county="Knox", raw_text=raw)
    _parse_name(n)
    _parse_address(n)
    _parse_pr_address(n)
    return n


class TestPersonalRepresentative:
    def test_block_layout_takes_the_human_not_the_court(self):
        """The prose 'issued to the referenced Personal Representative by the
        Chancery Court' must not win over the named PR further down."""
        n = _parse(NOTICE_TO_CREDITORS)
        assert n.owner_name == "Eric Lee Sharp", n.owner_name

    def test_court_never_lands_in_owner_name(self):
        n = _parse(NOTICE_TO_CREDITORS)
        assert "chancery" not in n.owner_name.lower()
        assert "court" not in n.owner_name.lower()

    def test_name_before_role_in_re(self):
        n = _parse(SERVICE_BY_PUBLICATION_INRE)
        assert n.owner_name.lower() == "brent baxley", n.owner_name

    def test_name_before_role_by(self):
        n = _parse(SERVICE_BY_PUBLICATION_BY)
        assert n.owner_name.lower() == "lee roy mcmahan", n.owner_name

    def test_pr_is_never_the_decedent(self):
        for raw in (NOTICE_TO_CREDITORS, SERVICE_BY_PUBLICATION_INRE, SERVICE_BY_PUBLICATION_BY):
            n = _parse(raw)
            if n.owner_name and n.decedent_name:
                assert n.owner_name.lower() != n.decedent_name.lower()


class TestDecedent:
    def test_decedent_from_estate_of_who_died(self):
        n = _parse(NOTICE_TO_CREDITORS)
        assert n.decedent_name.lower() == "doris o. young", n.decedent_name

    def test_decedent_from_deceased_marker(self):
        n = _parse(SERVICE_BY_PUBLICATION_INRE)
        assert "lucy ann glenn" in n.decedent_name.lower(), n.decedent_name


class TestPropertyAddress:
    @pytest.mark.parametrize(
        "raw", [NOTICE_TO_CREDITORS, SERVICE_BY_PUBLICATION_INRE, SERVICE_BY_PUBLICATION_BY]
    )
    def test_probate_never_takes_an_address_from_the_body(self, raw):
        """Probate bodies only ever contain court/attorney/PR addresses. The
        subject property is resolved later by property_lookup."""
        n = _parse(raw)
        assert n.address == "", f"took {n.address!r} out of a probate body"

    def test_courthouse_address_specifically_rejected(self):
        n = _parse(SERVICE_BY_PUBLICATION_BY)
        assert "400 W. Main" not in (n.address or "")

    def test_foreclosure_addresses_still_parse(self):
        """The probate short-circuit must not affect other notice types."""
        raw = (
            "SUBSTITUTE TRUSTEE'S SALE\n"
            "The property is commonly known as 158 Old State Rd, Knoxville, TN 37914."
        )
        n = NoticeData(notice_type="foreclosure", county="Knox", raw_text=raw)
        _parse_address(n)
        assert "158 Old State" in n.address, n.address


class TestPrMailingAddress:
    def test_pr_mailing_address_still_captured(self):
        """The PR's own address is useful (it is where they get mail) and is a
        different field from the subject property."""
        n = _parse(NOTICE_TO_CREDITORS)
        assert "806 Sunnydale" in n.owner_street, n.owner_street
        assert n.owner_zip == "37923", n.owner_zip


class TestLeadingSentenceWords:
    """A capital-letter name pattern cannot tell a sentence word from a first
    name, so "...BY: From Felicia F. Coalson, Personal Representative..." once
    uploaded a PR literally named "From Felicia F. Coalson"."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("From Felicia F. Coalson", "Felicia F. Coalson"),
            ("BY: LEE ROY MCMAHAN", "Lee Roy Mcmahan"),
            ("In Re: Brent Baxley", "Brent Baxley"),
            ("and John Smith", "John Smith"),
            ("To Lisa Bullock", "Lisa Bullock"),
        ],
    )
    def test_leading_filler_is_stripped(self, raw, expected):
        from notice_parser import _clean_name
        assert _clean_name(raw) == expected

    def test_real_names_untouched(self):
        from notice_parser import _clean_name
        assert _clean_name("ERIC LEE SHARP") == "Eric Lee Sharp"
        assert _clean_name("Doris O. Young") == "Doris O. Young"

    def test_never_strips_the_only_token(self):
        """A one-word capture must survive, so a surname colliding with a
        stopword cannot be erased into an empty string."""
        from notice_parser import _clean_name
        assert _clean_name("Estate") == "Estate"
