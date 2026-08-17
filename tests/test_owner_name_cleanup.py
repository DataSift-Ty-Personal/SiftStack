"""Owner names must not carry legal status words.

From a staged backfill month (Sep 2025, 134 records) these shipped as real
owner names, which is what a mail merge would have greeted people as:

    John Deceased        (from "JOHN SMITH, DECEASED")
    Patricia Married     (from "PATRICIA JONES, MARRIED")
    Grantee Trustee      x7
    Robert Lee JR.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

from notice_parser import _clean_name, _is_valid_name  # noqa: E402


class TestTrailingStatusWords:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("JOHN SMITH, DECEASED", "John Smith"),
            ("PATRICIA JONES, MARRIED", "Patricia Jones"),
            ("MARK DAVIS, married person", "Mark Davis"),
            ("ROBERT LEE JR.", "Robert Lee"),
            ("SARAH KING, unmarried", "Sarah King"),
            ("JAMES REED, widower", "James Reed"),
            ("ANNA HALL et al", "Anna Hall"),
        ],
    )
    def test_status_words_are_stripped(self, raw, expected):
        assert _clean_name(raw) == expected

    def test_real_names_survive(self):
        assert _clean_name("ERIC LEE SHARP") == "Eric Lee Sharp"
        assert _clean_name("Doris O. Young") == "Doris O. Young"

    def test_never_strips_the_only_token(self):
        assert _clean_name("Deceased") == "Deceased"


class TestRoleWordsRejected:
    @pytest.mark.parametrize(
        "raw", ["GRANTEE, TRUSTEE", "TRUSTEE", "Grantor", "MORTGAGOR", "the borrower"]
    )
    def test_role_word_is_not_a_valid_name(self, raw):
        """A rejected capture falls through to the next pattern rather than
        shipping a role as a person."""
        assert not _is_valid_name(_clean_name(raw))

    def test_names_that_merely_start_like_a_role_survive(self):
        """'Grantham' must not be eaten by the 'grantee'/'grantor' guard."""
        assert _is_valid_name(_clean_name("Grantham Miller"))
        assert _is_valid_name(_clean_name("Trusten Baker"))
