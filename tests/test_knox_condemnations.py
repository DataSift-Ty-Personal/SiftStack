"""Knox condemnation agenda parsing.

The bug these guard against: the property address was being taken as the last
house-numbered line before the parcel marker, which walks back into the PREVIOUS
dossier and lands on a party's MAILING address. A live June 2026 agenda parsed
as "14160 Dallas Parkway Suite 900" (a Dallas law firm) and three owners' home
addresses instead of the five condemned properties.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from knox_condemnations import _address_from_head, _build_note  # noqa: E402

# Shape of the text between one parcel marker and the next: the tail of the
# previous dossier (parties with mailing addresses), then the item letter, then
# the real property address.
HEAD_LETTER_THEN_ADDRESS = """
MACKIE WOLF ZIENTZ & MANN PC
14160 DALLAS PARKWAY SUITE 900
DALLAS TEXAS 75254-4314

OWNER SINCE: 05/28/2015
RECORD NUMBER: CE-26-000170

B.

1620 JEFFERSON AVENUE INCLUDING ACCESSORY STRUCTURE

"""

HEAD_ADDRESS_ON_LETTER_LINE = """
KATHY H & GORDON D FOSTER
6721 DEANE HILL DRIVE
KNOXVILLE TENNESSEE 37919-5941

C.             2738 JEFFERSON AVENUE

"""

HEAD_NO_LETTER = """
SOME PARTY
620 MILLIGAN STREET
KNOXVILLE TENNESSEE 37914
"""


class TestAddressExtraction:
    def test_address_on_the_line_after_the_letter(self):
        assert _address_from_head(HEAD_LETTER_THEN_ADDRESS) == \
            "1620 JEFFERSON AVENUE INCLUDING ACCESSORY STRUCTURE"

    def test_address_on_the_same_line_as_the_letter(self):
        assert _address_from_head(HEAD_ADDRESS_ON_LETTER_LINE) == "2738 JEFFERSON AVENUE"

    def test_never_returns_a_prior_party_mailing_address(self):
        """The whole point: everything before the item letter is the previous
        dossier and must not be reachable."""
        got = _address_from_head(HEAD_LETTER_THEN_ADDRESS)
        assert "DALLAS" not in got.upper()
        assert "14160" not in got

    def test_second_case_ignores_the_owner_mailing_address(self):
        got = _address_from_head(HEAD_ADDRESS_ON_LETTER_LINE)
        assert "DEANE HILL" not in got.upper()

    def test_no_item_letter_yields_nothing(self):
        """Better an empty address than a confidently wrong one."""
        assert _address_from_head(HEAD_NO_LETTER) == ""

    def test_empty_input(self):
        assert _address_from_head("") == ""


class TestNote:
    def test_note_carries_the_distress_detail(self):
        note = _build_note({
            "kind": "bbb", "hearing": "2026-06-25", "parcel": "070IF004",
            "violations": "EXTERIOR, ELECTRICAL, PLUMBING",
            "fees": "7 LIENS = $2,406.50", "owner_since": "04/10/1991",
            "city_tax": "PAID", "cty_tax": "PAID", "record": "CE-20-002882",
            "deceased_parties": 7, "has_unknown_heirs": True,
        })
        for bit in ("Certified blight", "070IF004", "ELECTRICAL", "2,406.50",
                    "CE-20-002882", "7 deceased", "Unknown or unborn heirs"):
            assert bit in note, f"{bit!r} missing from: {note}"

    def test_fees_none_is_not_reported_as_a_fee(self):
        note = _build_note({"kind": "poh", "hearing": "", "fees": "NONE",
                            "deceased_parties": 0, "has_unknown_heirs": False})
        assert "Fees" not in note

    def test_public_officer_labelled_distinctly(self):
        note = _build_note({"kind": "poh", "hearing": "2026-06-26",
                            "deceased_parties": 0, "has_unknown_heirs": False})
        assert "Public Officer" in note


# ── The layout the City adopted with its new website (October 2026) ───────
#
# Until 2026-11-11 the fetcher read two fixed URLs on the City's OLD host. They
# kept answering 200 with a valid PDF, so the capture reported OK every day,
# but the content had been the June agendas for months. The live agendas had
# moved to a new site and a new layout: labelled lines, "CLT NUMBER" for the
# parcel, and a boarding table extracted column by column. The text below has
# that exact shape; every name, address and number is invented.

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

import knox_condemnations as kc  # noqa: E402
from knox_ftm_pull import _condemnation_money  # noqa: E402

BULLET = chr(8226)

V2_AGENDA = f"""PUBLIC OFFICER HEARING
MEETING AGENDA
OCTOBER 30, 2026
SMALL ASSEMBLY ROOM, 9:30AM
The  City  of  Knoxville  Public  Officer  will  consider  the  following  petitions [...]
I.
II.
Call To Order
Old Business
A.  Review of September 25, 2026 Meeting
III.    New Business/ Orders
Public Officer
{BULLET}  100 EXAMPLE AVENUE
{BULLET}  200 SAMPLE ROAD
IV.    Boarding Approvals
Public Officer
ADDRESS
11 FIRST LANE
22 SECOND AVENUE
33 THIRD PIKE
DATE BOARDED
10/02/2026
09/17/2026
07/01/2026
NEW BUSINESS PETITION DETAILS
ADDRESS:  100 EXAMPLE AVENUE
RECORD NUMBER:  CE-26-000001
CLT NUMBER:  070AA001
ZONING:  RN-2
CITY TAXES:  $633.85 (2025)
COUNTY TAXES:  $442.22 (2025)
CODE FEES:  N/A
OFFICER:  A. OFFICER
PERMITS:  N/A
FIRST INSPECTION:  09/08/2026
CONDEMNATION:  N/A
LAST INSPECTION:  10/19/2026
OWNER SINCE:  12/20/1978
ROD COMPLAINT INSTRUMENT:  202610150000001
OWNERS AND OTHER INTERESTED PARTIES:
 PAT A SAMPLE
{BULLET}
{BULLET}  JORDAN A SAMPLE, DECEASED
VIOLATIONS:  STRUCTURAL
ADDRESS:  200 SAMPLE ROAD
CLT NUMBER:  081BB00201
ZONING:  RN-1
CITY TAXES:  $371.52 (2026)
COUNTY TAXES:  $270.00 (2026)
RECORD NUMBER:  CE-25-000002
OFFICER:  B. OFFICER
PERMITS:  N/A
FIRST INSPECTION:  06/09/2026
CONDEMNATION:  N/A
CODE FEES:  BOARDING BILL $208.00
LAST INSPECTION:  10/19/2026
OWNER SINCE:  07/27/2023
ROD COMPLAINT INSTRUMENT:  202610150000002
OWNERS AND OTHER INTERESTED PARTIES:
{BULLET}  RILEY & CASEY EXAMPLE
{BULLET}  EXAMPLE SERVICING INC
VIOLATIONS:  BURNOUT
"""

CANCELED_NOTICE = """THE OCTOBER 29, 2026
BETTER BUILDING BOARD
MEETING HAS BEEN CANCELED.
THE DECEMBER 3, 2026 MEETING
AGENDA WILL BE POSTED ONE
WEEK BEFORE THE MEETING.
"""


class TestNewLayout:
    def test_every_dossier_is_found(self):
        recs = kc.parse_agenda_text(V2_AGENDA, "poh", "poh_x.pdf")
        assert [r["address"] for r in recs] == ["100 EXAMPLE AVENUE", "200 SAMPLE ROAD"]

    def test_the_summary_bullets_are_not_counted_as_dossiers(self):
        """The address also appears in the New Business list near the top. Only
        the labelled 'ADDRESS:' line opens a dossier, or every property doubles."""
        assert len(kc.parse_agenda_text(V2_AGENDA, "poh")) == 2

    def test_fields_come_from_the_new_labels(self):
        a, b = kc.parse_agenda_text(V2_AGENDA, "poh")
        assert a["parcel"] == "070AA001" and b["parcel"] == "081BB00201"
        assert a["record"] == "CE-26-000001" and b["record"] == "CE-25-000002"
        assert a["hearing"] == b["hearing"] == "2026-10-30"
        assert a["owner_since"] == "12/20/1978"
        assert a["inspected"] == "09/08/2026"
        assert a["violations"] == "STRUCTURAL" and b["violations"] == "BURNOUT"
        assert b["fees"] == "BOARDING BILL $208.00"

    def test_parties_stay_with_their_own_dossier(self):
        a, b = kc.parse_agenda_text(V2_AGENDA, "poh")
        assert a["parties"] == ["PAT A SAMPLE", "JORDAN A SAMPLE, DECEASED"]
        assert a["owner_raw"] == "PAT A SAMPLE"
        assert a["deceased_parties"] == 1
        assert b["parties"] == ["RILEY & CASEY EXAMPLE", "EXAMPLE SERVICING INC"]
        assert b["deceased_parties"] == 0

    def test_money_survives_into_the_structured_fields(self):
        """The note is what knox_ftm_pull reads the dollar figures out of. With
        the old wording-only patterns the new agendas uploaded blank."""
        a, b = kc.parse_agenda_text(V2_AGENDA, "poh")
        assert _condemnation_money(a["notes"]) == {
            "tax_due": 1076.07, "tax_years": [2025], "lien_total": None}
        assert _condemnation_money(b["notes"]) == {
            "tax_due": 641.52, "tax_years": [2026], "lien_total": 208.0}

    def test_old_wording_still_parses_and_is_not_double_counted(self):
        got = _condemnation_money(
            "Fees: 2 liens = $422.00. City taxes: City tax: $3,884.46 (2022), "
            "county $2,113.47 (2022). 1 bill = $254.00.")
        assert got == {"tax_due": 5997.93, "tax_years": [2022], "lien_total": 676.0}

    def test_boarding_table_pairs_addresses_with_dates_by_position(self):
        rows = kc.parse_boardings_text(V2_AGENDA)
        assert [(r["address"], r["boarded"]) for r in rows] == [
            ("11 FIRST LANE", "10/02/2026"),
            ("22 SECOND AVENUE", "09/17/2026"),
            ("33 THIRD PIKE", "07/01/2026"),
        ]

    def test_boarding_dates_are_dropped_when_the_columns_do_not_line_up(self):
        """A date on the wrong house is worse than no date."""
        broken = V2_AGENDA.replace("07/01/2026\nNEW BUSINESS", "NEW BUSINESS")
        rows = kc.parse_boardings_text(broken)
        assert [r["address"] for r in rows] == ["11 FIRST LANE", "22 SECOND AVENUE", "33 THIRD PIKE"]
        assert all(r["boarded"] == "" for r in rows)

    def test_boardings_do_not_swallow_the_petition_addresses(self):
        addrs = [r["address"] for r in kc.parse_boardings_text(V2_AGENDA)]
        assert "100 EXAMPLE AVENUE" not in addrs


class TestCanceledAndStale:
    def test_a_cancellation_notice_is_recognized(self):
        assert kc.is_canceled_text(CANCELED_NOTICE)

    def test_a_real_agenda_is_never_mistaken_for_a_cancellation(self):
        assert not kc.is_canceled_text(V2_AGENDA)
        assert not kc.is_canceled_text(V2_AGENDA + "\nItem C was CANCELED by the owner.")

    def test_newest_date_reads_any_date_in_the_document(self):
        assert kc.newest_date_text(CANCELED_NOTICE) == "2026-12-03"
        assert kc.newest_date_text(V2_AGENDA) == "2026-10-30"
        assert kc.newest_date_text("no dates here") == ""

    def test_board_page_links_are_matched_on_their_text(self):
        html = (
            '<a href="/DocumentCenter/View/9001" target="_blank">View Current Better '
            'Building Board Agenda (PDF)</a>'
            '<a href="/DocumentCenter/View/9002" class="x"><span>Public Officer Hearing '
            'Current Agenda (PDF)</span></a>'
            '<a href="/DocumentCenter/View/7">Board Membership (PDF)</a>'
        )
        assert kc.links_from_board_page(html) == {
            "bbb": kc.CITY + "/DocumentCenter/View/9001",
            "poh": kc.CITY + "/DocumentCenter/View/9002",
        }

    def test_a_refused_direct_fetch_falls_back_to_a_residential_address(self):
        """The City's site answers 403 to a datacenter address (the Fly box,
        2026-11-12). The free fetch is tried first; the paid one only on refusal."""
        calls = []

        def refused(url):
            calls.append(("direct", url))
            raise RuntimeError("403 Client Error: Forbidden")

        def residential(url):
            calls.append(("residential", url))
            return b"%PDF-1.7 agenda"

        assert kc.http_get("https://example.invalid/a.pdf", direct=refused,
                           residential=residential) == b"%PDF-1.7 agenda"
        assert [c[0] for c in calls] == ["direct", "residential"]

    def test_a_served_direct_fetch_never_pays_for_the_fallback(self):
        def residential(url):
            raise AssertionError("the residential fetch must not run")

        assert kc.http_get("https://example.invalid/a.pdf", direct=lambda u: b"ok",
                           residential=residential) == b"ok"

    def test_a_refusal_with_no_fallback_configured_still_raises(self, monkeypatch):
        monkeypatch.delenv("SCRAPFLY_KEY", raising=False)

        def refused(url):
            raise RuntimeError("403 Client Error: Forbidden")

        with pytest.raises(RuntimeError):
            kc.http_get("https://example.invalid/a.pdf", direct=refused)

    def test_stale_documents_are_a_problem(self, monkeypatch):
        """The check the old fixed URL never had. June agendas read in November
        must fail the stage, however healthy the download looked."""
        june = V2_AGENDA.replace("OCTOBER 30, 2026", "JUNE 26, 2026").replace(
            "September 25, 2026", "May 29, 2026")
        monkeypatch.setattr(kc, "_text", lambda path: june)
        probs = kc.staleness_problems({"poh": "x.pdf"}, today=datetime(2026, 11, 11))
        assert len(probs) == 1 and "STALE" in probs[0] and "2026-06-26" in probs[0]

    def test_current_documents_are_not_flagged(self, monkeypatch):
        monkeypatch.setattr(kc, "_text", lambda path: V2_AGENDA)
        assert kc.staleness_problems({"poh": "x.pdf"}, today=datetime(2026, 11, 11)) == []

    def test_collect_treats_a_canceled_board_as_no_problem(self, monkeypatch):
        texts = {"bbb.pdf": CANCELED_NOTICE, "poh.pdf": V2_AGENDA}
        monkeypatch.setattr(kc, "_text", lambda path: texts[path])
        fresh, problems = kc.collect({"bbb": "bbb.pdf", "poh": "poh.pdf"},
                                     today=datetime(2026, 11, 11))
        assert problems == []
        assert len(fresh) == 5      # 2 petitions + 3 boardings

    def test_collect_flags_a_missing_or_unparsed_agenda(self, monkeypatch):
        texts = {"poh.pdf": "PUBLIC OFFICER HEARING\nMEETING AGENDA\nOCTOBER 30, 2026\nnew layout nobody has seen"}
        monkeypatch.setattr(kc, "_text", lambda path: texts[path])
        fresh, problems = kc.collect({"poh": "poh.pdf"}, today=datetime(2026, 11, 11))
        assert fresh == []
        assert any("bbb agenda was not fetched" in p for p in problems)
        assert any("poh agenda parsed 0 entries" in p for p in problems)
