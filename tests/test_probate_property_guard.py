"""Probate property lookup: the owner-of-record guard and the ZIP fallback.

Zero network. Every case here is a failure that reached production:

  * The assessor search matches loosely and the lookup kept the first
    residential row without checking who owned it, so a damaged decedent name
    resolved to a stranger's house and uploaded as the estate's property.
  * The county lookups return a street line only and the ZIP came from Smarty
    alone, so when that subscription lapsed every probate record was dropped
    at validation for a missing ZIP while the run still reported OK.

Names and addresses below are invented. The shapes are the real ones: TPAD
writes owners LAST FIRST with spouses joined by "&".
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest  # noqa: E402

import address_fallback as af  # noqa: E402
import property_lookup as pl  # noqa: E402


class TestOwnerOfRecordGuard:
    @pytest.mark.parametrize(
        "decedent, owner",
        [
            ("Nora Faye Tindle", "TINDLE NORA FAYE "),
            ("Nora Faye Tindle", "TINDLE ROBERT L & NORA F "),
            ("NORA FAYE TINDLE", "TINDLE NORA"),
            ("Ray Wendell Brannock", "BRANNOCK RAY W "),
            # Maiden name on the roll: FIRST MIDDLE MAIDEN MARRIED.
            ("Opal Corinne Garland Jones", "GARLAND OPAL C"),
            # Alias carries the name the property is titled under.
            ("Lonny Dale Arbor Aka Lon Dale Arbor", "ARBOR LON D "),
            ("Mary Ann Smith a/k/a Mary Ann Brown", "BROWN MARY ANN"),
            # Hyphenated surname, generational suffix, punctuation.
            ("Mary Smith-Jones", "SMITH-JONES MARY"),
            ("Robert Lee Thornby Jr.", "THORNBY ROBERT LEE JR"),
            ("Wilma O. Yates", "YATES WILMA O"),
        ],
    )
    def test_the_decedent_is_accepted(self, decedent, owner):
        assert pl._owner_matches_decedent(decedent, owner)

    @pytest.mark.parametrize(
        "decedent, owner",
        [
            # The production failure: surname merely begins with the search
            # fragment and the first name is a near miss.
            ("Hanna B Thornby", "DEWITT ORSON BLAINE & HANNAH L MOSS"),
            ("Nora Faye Tindle", "WALKER NORA DENISE & GLEN "),
            ("Verna Kay Fennick", "DENHAM CURTIS ALAN & VERNA GAIL "),
            # Right surname, different person.
            ("Verna Kay Fennick", "FENNICK ROBERT & CAROL"),
            # "Ann" must not be satisfied by "Annette".
            ("Ann Marie Cole", "COLE ANNETTE"),
            # Entities and empties.
            ("Verna Kay Fennick", "FENNICK FAMILY TRUST"),
            ("Verna Kay Fennick", ""),
            ("", "FENNICK VERNA"),
            ("Fennick", "FENNICK VERNA"),
        ],
    )
    def test_everyone_else_is_rejected(self, decedent, owner):
        assert not pl._owner_matches_decedent(decedent, owner)

    def test_keep_owner_matches_filters_a_mixed_result_set(self):
        rows = [
            {"owner": "DENHAM CURTIS ALAN & VERNA GAIL", "address": "1 Wrong Rd"},
            {"owner": "FENNICK VERNA K", "address": "2 Right Rd"},
        ]
        kept = pl._keep_owner_matches(rows, "Verna Kay Fennick")
        assert [r["address"] for r in kept] == ["2 Right Rd"]

    def test_all_rejected_returns_empty_so_the_next_variant_runs(self):
        rows = [{"owner": "DENHAM CURTIS ALAN & VERNA GAIL", "address": "1 Wrong Rd"}]
        assert pl._keep_owner_matches(rows, "Verna Kay Fennick") == []


class TestSearchVariants:
    def test_alias_is_not_sent_to_the_assessor_as_one_name(self):
        queries = [q for _, q in pl._search_variants("Lonny Dale Arbor Aka Lon Dale Arbor")]
        assert queries[0] == "ARBOR LONNY DALE"
        assert "ARBOR LON DALE" in queries
        assert "ARBOR LON" in queries
        assert not any("AKA" in q for q in queries)

    def test_order_is_full_then_short_then_maiden(self):
        labels = [l for l, _ in pl._search_variants("Opal Corinne Garland Jones")]
        assert labels == ["", "shorter name", "maiden name"]

    def test_no_duplicate_queries(self):
        queries = [q for _, q in pl._search_variants("Nora Tindle aka Nora Tindle")]
        assert len(queries) == len(set(queries))

    def test_two_part_name_has_one_variant(self):
        assert pl._search_variants("Nora Tindle") == [("", "TINDLE NORA")]


class _StubClient:
    """Answers autocomplete from a fixed table and records what was asked."""

    def __init__(self, table):
        self.table = table
        self.asked = []

    def autocomplete(self, query, limit=8):
        self.asked.append(query)
        return self.table.get(query, [])


class _Notice:
    def __init__(self, address, city="", county="", zip_=""):
        self.address, self.city, self.county, self.zip, self.state = address, city, county, zip_, "TN"


def _hit(full):
    return {"address": full, "title": full}


class TestZipFallback:
    def test_street_key_ignores_case_suffix_spelling_and_unit(self):
        a = af.street_key("3320 OLD MILL ROAD")
        assert a == af.street_key("3320 Old Mill Rd")
        assert a == af.street_key("3320 Old Mill Rd Apt 4")
        assert af.street_key("12 West Main Street") == af.street_key("12 W Main St")
        assert af.street_key("3320 Old Mill Rd") != af.street_key("3322 Old Mill Rd")
        assert af.street_key("Old Mill Rd") == ("", frozenset())

    def test_zip_in_county(self):
        assert af.zip_in_county("37803", "Blount")
        assert af.zip_in_county("37853", "blount")
        assert not af.zip_in_county("37921", "Blount")
        assert af.zip_in_county("37921", "Knox")
        assert af.zip_in_county("37849", "Knox")
        assert not af.zip_in_county("38103", "Knox")
        assert af.zip_in_county("38103", "Shelby")

    def test_fills_city_and_zip_and_corrects_the_default_city(self):
        n = _Notice("9 EXAMPLE GROVE ST", city="Maryville", county="Blount")
        client = _StubClient({
            "9 EXAMPLE GROVE ST, Maryville, TN": [],
            "9 EXAMPLE GROVE ST TN": [_hit("9 Example Grove St, Rockford, TN 37853")],
        })
        assert af.fill_missing_city_zip([n], client=client) == 1
        assert (n.address, n.city, n.zip) == ("9 Example Grove St", "Rockford", "37853")

    def test_same_street_in_another_part_of_the_state_is_refused(self):
        n = _Notice("100 MAIN ST", city="Maryville", county="Blount")
        client = _StubClient({
            "100 MAIN ST, Maryville, TN": [_hit("100 Main St, Memphis, TN 38103")],
            "100 MAIN ST TN": [_hit("100 Main St, Memphis, TN 38103")],
        })
        assert af.fill_missing_city_zip([n], client=client) == 0
        assert n.zip == ""
        assert n.city == "Maryville"

    def test_wrong_house_number_and_wrong_state_are_refused(self):
        n = _Notice("100 MAIN ST", city="Maryville", county="Blount")
        client = _StubClient({
            "100 MAIN ST, Maryville, TN": [
                _hit("102 Main St, Maryville, TN 37801"),
                _hit("100 Main St, Maryville, GA 30501"),
                _hit("100 Maine St, Maryville, TN 37801"),
            ],
        })
        assert af.fill_missing_city_zip([n], client=client) == 0

    def test_a_record_that_already_has_a_zip_is_never_touched(self):
        n = _Notice("100 Main St", city="Knoxville", county="Knox", zip_="37902")
        client = _StubClient({})
        assert af.fill_missing_city_zip([n], client=client) == 0
        assert client.asked == []

    def test_a_record_with_no_street_is_never_looked_up(self):
        n = _Notice("", county="Blount")
        client = _StubClient({})
        assert af.fill_missing_city_zip([n], client=client) == 0
        assert client.asked == []

    def test_a_client_error_leaves_the_record_as_it_was(self):
        class Boom:
            def autocomplete(self, query, limit=8):
                raise RuntimeError("upstream down")

        n = _Notice("100 MAIN ST", city="Maryville", county="Blount")
        assert af.fill_missing_city_zip([n], client=Boom()) == 0
        assert (n.address, n.city, n.zip) == ("100 MAIN ST", "Maryville", "")


class TestKnoxSecondPassLookup:
    """tax_enricher's Step 3c runs AFTER property_lookup, on whatever it missed.

    It had no owner check of its own, so every honest miss upstream was handed
    to a looser matcher. Seen live 2026-11-11 on the Knox September probate
    run: all 8 executor-tier matches were the executor's own house, and one
    county result with a null address crashed the whole stage.
    """

    @pytest.fixture
    def te(self, monkeypatch):
        import tax_enricher

        monkeypatch.setattr(tax_enricher.time, "sleep", lambda s: None)
        monkeypatch.setattr(tax_enricher, "_people_search_property", lambda name, city="": None)
        return tax_enricher

    @staticmethod
    def _notice(decedent, executor):
        from notice_parser import NoticeData

        n = NoticeData()
        n.notice_type, n.county = "probate", "Knox"
        n.decedent_name, n.owner_name = decedent, executor
        return n

    @staticmethod
    def _serve(te, monkeypatch, by_query):
        """Answer the county name search from a table, scored like the real one."""
        def fake(name, min_score=0.4):
            rows = by_query.get(name.upper(), [])
            scored = [(te._name_match_score(name, p.get("owner") or ""), p) for p in rows]
            return sorted([s for s in scored if s[0] >= min_score], key=lambda x: -x[0])
        monkeypatch.setattr(te, "_knox_name_search", fake)

    def test_the_executors_own_house_is_not_the_estate(self, te, monkeypatch):
        # Daughter is the executor and shares the surname. Her own parcel carries
        # the decedent's surname, which is all the old tier asked for.
        own_home = {"parcel_address": "410 EXAMPLE RIDGE RD", "owner": "HOLLIS DANA M & TERRY",
                    "account_number": "A1"}
        self._serve(te, monkeypatch, {"DANA HOLLIS": [own_home], "HOLLIS DANA": [own_home]})
        n = self._notice("Carol Ann Hollis", "Dana Hollis")
        te._probate_property_lookup([n])
        assert n.address == ""

    def test_a_parcel_the_decedent_co_owns_is_found_through_the_executor(self, te, monkeypatch):
        joint = {"parcel_address": "77 SAMPLE CREEK LN", "owner": "HOLLIS DANA M & CAROL A",
                 "account_number": "A2"}
        self._serve(te, monkeypatch, {"DANA HOLLIS": [joint]})
        n = self._notice("Carol Ann Hollis", "Dana Hollis")
        te._probate_property_lookup([n])
        assert n.address == "77 SAMPLE CREEK LN"
        assert n.parcel_id == "A2"

    def test_a_surname_only_match_is_rejected(self, te, monkeypatch):
        # Two-word names overlap 1 of 2 tokens on the surname alone: score 0.5.
        stranger = {"parcel_address": "9 PLACEHOLDER CT", "owner": "TINDLE ROBERT",
                    "account_number": "A3"}
        self._serve(te, monkeypatch, {"NORA TINDLE": [stranger], "TINDLE NORA": [stranger]})
        n = self._notice("Nora Tindle", "")
        te._probate_property_lookup([n])
        assert n.address == ""

    def test_an_account_with_no_street_address_is_skipped_not_applied(self, te, monkeypatch):
        business = {"parcel_address": None, "owner": "TINDLE NORA FAYE", "account_number": "B9"}
        house = {"parcel_address": "12 INVENTED AVE", "owner": "TINDLE NORA FAYE", "account_number": "A4"}
        self._serve(te, monkeypatch, {"NORA FAYE TINDLE": [business, house]})
        n = self._notice("Nora Faye Tindle", "")
        te._probate_property_lookup([n])
        assert n.address == "12 INVENTED AVE"

    def test_only_a_null_address_account_leaves_the_address_a_string(self, te, monkeypatch):
        business = {"parcel_address": None, "owner": "TINDLE NORA FAYE", "account_number": "B9"}
        self._serve(te, monkeypatch, {"NORA FAYE TINDLE": [business]})
        n = self._notice("Nora Faye Tindle", "")
        te._probate_property_lookup([n])
        assert n.address == ""          # never None: every later step calls .strip()

    def test_one_record_that_raises_does_not_stop_the_rest(self, te, monkeypatch):
        good = {"parcel_address": "12 INVENTED AVE", "owner": "TINDLE NORA FAYE", "account_number": "A4"}

        def fake(name, min_score=0.4):
            if "BOOM" in name.upper():
                raise RuntimeError("county API fell over")
            return [(1.0, good)]
        monkeypatch.setattr(te, "_knox_name_search", fake)
        first, second = self._notice("Boom Case", ""), self._notice("Nora Faye Tindle", "")
        te._probate_property_lookup([first, second])
        assert first.address == "" and second.address == "12 INVENTED AVE"
