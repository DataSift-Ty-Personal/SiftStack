"""Name splitting for DataSift upload.

A spelled-out middle name was being folded into the surname: the live probate
record for personal representative "ERIC LEE SHARP" uploaded as
first="Eric", last="Lee Sharp", which breaks record matching, mail merge and
any skip trace keyed on the surname.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

from datasift_formatter import _clean_and_split_name as split  # noqa: E402


class TestMiddleNames:
    def test_spelled_out_middle_name_is_not_surname(self):
        assert split("Eric Lee Sharp") == ("Eric", "Sharp")

    def test_middle_initial_still_stripped(self):
        assert split("Eric J. Yopp") == ("Eric", "Yopp")

    def test_two_middle_names(self):
        assert split("John Paul George Ringo") == ("John", "Ringo")

    def test_simple_two_part_name_unchanged(self):
        assert split("Doris Young") == ("Doris", "Young")

    def test_single_token(self):
        assert split("Cher") == ("Cher", "")


class TestSurnameParticles:
    @pytest.mark.parametrize(
        "full,first,last",
        [
            ("Ann Van Buren", "Ann", "Van Buren"),
            ("Maria De La Cruz", "Maria", "De La Cruz"),
            ("Jose Del Rio", "Jose", "Del Rio"),
            ("Patrick Mac Donald", "Patrick", "Mac Donald"),
            ("Ludwig Von Mises", "Ludwig", "Von Mises"),
        ],
    )
    def test_multi_word_surnames_stay_whole(self, full, first, last):
        assert split(full) == (first, last)

    def test_st_prefix_survives_initial_stripping(self):
        """'Richard C. St. Leger', the initial goes, the particle stays."""
        assert split("Richard C. St. Leger") == ("Richard", "St. Leger")


class TestExistingBehaviourPreserved:
    def test_joint_owners_keep_first_person(self):
        assert split("John & Jane Smith") == ("John", "Smith")

    def test_joint_owners_with_and(self):
        assert split("John AND Jane Smith") == ("John", "Smith")

    def test_entity_returns_empty(self):
        assert split("Volunteer Home Buyers LLC") == ("", "")

    def test_empty_input(self):
        assert split("") == ("", "")
