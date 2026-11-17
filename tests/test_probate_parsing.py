"""Probate notice parsing, regression tests from live Knox notices.

Every fixture below keeps the layout and wording of a notice scraped from
tnpublicnotice.com on 2026-08-14, trimmed only where marked. The people,
addresses, docket numbers and notice ids are fictional, since this repo is
public. Each one encodes a bug that shipped records into DataSift:

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


# Knox, ID 554613, published 2026-08-03. Middle paragraphs of statutory
# boilerplate trimmed; the prose mention of the role is kept because it is the
# thing that used to win.
NOTICE_TO_CREDITORS = """NOTICE TO CREDITORS

ESTATE OF LORENE O. BASKIN
DOCKET NUMBER 93417-2
Notice is hereby given that on the 27th day of July, 2026, Letters Testamentary in respect to the Estate of LORENE O. BASKIN, who died on April 13, 2026, were issued to the referenced Personal Representative by the Chancery Court, Probate Division, of Knox County, Tennessee. All persons, resident and non-resident, having claims, matured or unmatured, against the estate are required to file the same with the clerk of the above-named court on or before the earlier of the dates prescribed in (1) or (2), otherwise their claims will be forever barred:
(2) Twelve (12) months from the decedent's date of death.
This the 27th day of JULY, 2026.

ESTATE OF LORENE O. BASKIN

PERSONAL REPRESENTATIVE(S)
WENDELL LEE HARLOW
806 EXAMPLE ROAD
KNOXVILLE, TN 37923

RAYMOND J. PELLETIER, ATTORNEY
445 S. EXAMPLE STREET, SUITE 401
KNOXVILLE, TN 37902
"""

# Knox, ID 554527, published 2026-08-10. Names the PR before the role.
SERVICE_BY_PUBLICATION_INRE = """NOTICE OF SERVICE BY PUBLICATION

TO: TRAVIS PRESNELL, AUTUMN BLEVINS, MORGAN PRESNELL, and ANY UNKNOWN HEIRS of
NELLIE ANN PRESNELL (deceased)

IN RE: GRANT TILLERY, Personal Representative
Vs.
TRAVIS PRESNELL, AUTUMN BLEVINS, MORGAN PRESNELL, and ANY UNKNOWN HEIRS of NELLIE ANN PRESNELL (deceased), Defendants

No. 79264-3
IN THE CHANCERY COURT FOR KNOX COUNTY, TENNESSEE, PROBATE DIVISION
Therefore, it is ordered that said Defendants file an answer with the Clerk & Master of the Chancery Court (Probate Division) at Knoxville, Tennessee and with L. Dwayne Ostrander, Petitioner's attorney, whose address is The Ostrander Law Firm, 9145 Example Park Dr, Suite 103, Knoxville, TN 37923.

Hon. Malcolm D. Prewitt
Chancellor
"""

# Knox, ID 554582, published 2026-08-03. States the courthouse address.
SERVICE_BY_PUBLICATION_BY = """NOTICE OF SERVICE BY PUBLICATION

TO: TONYA BURCHETT

IN RE: THE ESATE OF JACK EUGENE MCCARTER

BY: LEE ROY MCCARTER, Personal Representative / Petitioner
Vs.
TONYA BURCHETT, Respondent

No. 91482-1

IN THE CHANCERY COURT FOR KNOX COUNTY, TENNESSEE,
PROBATE DIVISION
Therefore, it is ORDERED that TONYA BURCHETT, Respondent, appear before the Chancery Court for Knox County, Tennessee, Probate Division, located at 400 W. Main Street, Suite 125, Knoxville, Tennessee 37902, on the 17th day of September, 2026, at 9:30 a.m., to respond to the Petition.

Honorable Philip F. Ransome
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
        assert n.owner_name == "Wendell Lee Harlow", n.owner_name

    def test_court_never_lands_in_owner_name(self):
        n = _parse(NOTICE_TO_CREDITORS)
        assert "chancery" not in n.owner_name.lower()
        assert "court" not in n.owner_name.lower()

    def test_name_before_role_in_re(self):
        n = _parse(SERVICE_BY_PUBLICATION_INRE)
        assert n.owner_name.lower() == "grant tillery", n.owner_name

    def test_name_before_role_by(self):
        n = _parse(SERVICE_BY_PUBLICATION_BY)
        assert n.owner_name.lower() == "lee roy mccarter", n.owner_name

    def test_pr_is_never_the_decedent(self):
        for raw in (NOTICE_TO_CREDITORS, SERVICE_BY_PUBLICATION_INRE, SERVICE_BY_PUBLICATION_BY):
            n = _parse(raw)
            if n.owner_name and n.decedent_name:
                assert n.owner_name.lower() != n.decedent_name.lower()


class TestDecedent:
    def test_decedent_from_estate_of_who_died(self):
        n = _parse(NOTICE_TO_CREDITORS)
        assert n.decedent_name.lower() == "lorene o. baskin", n.decedent_name

    def test_decedent_from_deceased_marker(self):
        n = _parse(SERVICE_BY_PUBLICATION_INRE)
        assert "nellie ann presnell" in n.decedent_name.lower(), n.decedent_name


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
            "The property is commonly known as 158 Old Example Rd, Knoxville, TN 37914."
        )
        n = NoticeData(notice_type="foreclosure", county="Knox", raw_text=raw)
        _parse_address(n)
        assert "158 Old Example" in n.address, n.address


class TestPrMailingAddress:
    def test_pr_mailing_address_still_captured(self):
        """The PR's own address is useful (it is where they get mail) and is a
        different field from the subject property."""
        n = _parse(NOTICE_TO_CREDITORS)
        assert "806 Example" in n.owner_street, n.owner_street
        assert n.owner_zip == "37923", n.owner_zip


class TestLeadingSentenceWords:
    """A capital-letter name pattern cannot tell a sentence word from a first
    name, so "...BY: From Rhonda F. Gilliam, Personal Representative..." once
    uploaded a PR literally named "From Rhonda F. Gilliam"."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("From Rhonda F. Gilliam", "Rhonda F. Gilliam"),
            ("BY: LEE ROY MCCARTER", "Lee Roy Mccarter"),
            ("In Re: Grant Tillery", "Grant Tillery"),
            ("and John Smith", "John Smith"),
            ("To Tonya Burchett", "Tonya Burchett"),
        ],
    )
    def test_leading_filler_is_stripped(self, raw, expected):
        from notice_parser import _clean_name
        assert _clean_name(raw) == expected

    def test_real_names_untouched(self):
        from notice_parser import _clean_name
        assert _clean_name("WENDELL LEE HARLOW") == "Wendell Lee Harlow"
        assert _clean_name("Lorene O. Baskin") == "Lorene O. Baskin"

    def test_never_strips_the_only_token(self):
        """A one-word capture must survive, so a surname colliding with a
        stopword cannot be erased into an empty string."""
        from notice_parser import _clean_name
        assert _clean_name("Estate") == "Estate"


# Blount County, published 2026-11-10 in a narrow newspaper column and served
# as PDF text. The layout, the line breaks and the mid-word hyphens are exactly
# as scraped; the NAMES are changed, since this repo is public. Statutory
# boilerplate trimmed where marked.
BLOUNT_WRAPPED = """NOTICE TO CREDITORS

 DOCKET NO.: PR-0000
(As required by section 30-2-
306 of the Tennessee Code
Annotated) Estate of Maria B
Hale, late of  Blount County,
Tennessee.

Notice  is  hereby  given  that  on
November  3rd,  2026,  Letters
Testamentary in respect to the
Estate  of  Maria  B  Hale,  de-
ceased,  who  died  on  October
19th, 2026, were issued to the
undersigned by Probate Clerk of
Blount  County,  Tennessee.  All
persons, resident and non-resi-
dent, having claims, matured or
unmatured, against said Estate
are required to file the same [...]

This  the  3  day  of  November,
2026.

Edwin F Hale
Personal Representative

By: Clerk Name,
Blount County Probate Clerk
Maryville, TN 37804
"""

# Same layout, the break falls inside the decedent's own name, twice.
BLOUNT_WRAPPED_NAME = """NOTICE TO CREDITORS

(As required by section 30-2-
306 of the Tennessee Code
Annotated) Estate of Karen Di-
anne Westmore, late of  Blount
County, Tennessee.

Notice  is  hereby  given  that  on
November  3rd,  2026,  Letters
Testamentary in respect to the
Estate  of  Karen  Dianne  West-
more, deceased, who died on
October 2nd, 2026, were issued [...]
"""

NL = chr(10)


class TestLineWrappedNames:
    """A column break inside a word must not become part of a name.

    What shipped before: "Estate of NAME, de-" then "ceased, who died" on the
    next line never matched the Deceased anchor, the capture ran on, and the
    decedent was stored as "Name, De- Ceased". The assessor was then searched
    for "DE- FIRST" and returned an unrelated family's house.
    """

    def test_deceased_split_across_lines_is_not_part_of_the_name(self):
        n = _parse(BLOUNT_WRAPPED)
        assert n.decedent_name == "Maria B Hale"

    def test_name_split_across_lines_is_rejoined(self):
        n = _parse(BLOUNT_WRAPPED_NAME)
        assert n.decedent_name == "Karen Dianne Westmore"

    def test_no_fragment_survives_in_any_form(self):
        for raw in (BLOUNT_WRAPPED, BLOUNT_WRAPPED_NAME):
            name = _parse(raw).decedent_name
            assert "-" not in name
            assert "ceased" not in name.lower()

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("de-" + NL + "ceased", "deceased"),
            ("STRICK-" + NL + "LAND", "STRICKLAND"),
            ("non-resi-" + NL + "dent", "non-resident"),
            ("publica- " + NL + " tion", "publication"),
            # A real hyphenated surname broken at its own hyphen keeps it.
            ("Smith-" + NL + "Jones", "Smith-Jones"),
            # Digits are a statute citation, not a wrapped word.
            ("30-2-" + NL + "306", "30-2-" + NL + "306"),
            # A hyphen that is not at a line end is left alone.
            ("Mary Smith-Jones", "Mary Smith-Jones"),
        ],
    )
    def test_join_line_wraps(self, raw, expected):
        from notice_parser import _join_line_wraps
        assert _join_line_wraps(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("KAREN DIANNE WEST- MORE", "Karen Dianne Westmore"),
            ("Maria B Hale, de- ceased", "Maria B Hale"),
            ("Mary Smith- Jones", "Mary Smith-Jones"),
        ],
    )
    def test_clean_name_repairs_a_wrap_that_reached_it(self, raw, expected):
        from notice_parser import _clean_name
        assert _clean_name(raw) == expected
