"""knox_tax_sale and code_violations: the two county feeds that arrive as documents.

Zero network. The Trustee's page, its PDFs and the county's parcel lookup are fakes; the code
violation export is written into a temp inbox. Every parcel id, address and name is invented.

Run: python -m pytest tests/test_knox_tax_sale.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import code_violations as C  # noqa: E402
import ftm_staging  # noqa: E402
import knox_tax_sale as T  # noqa: E402

PAGE = "https://trustee.example.invalid/services/tax-sale"
LIST_PDF = b"%PDF-1.4 list"
RESULT_PDF = b"%PDF-1.4 results"

HTML = """
<html><body>
 <a href="/assets/pdfs/TaxSale99-PropertyList.pdf">Tax Sale 99 <b>Property List</b></a>
 <a href="assets/pdfs/TaxSale98-FINAL_RESULTS.pdf">Tax Sale 98 Final Results</a>
 <a href="/assets/pdfs/TS99_Terms.pdf">Terms and Conditions</a>
 <a href="/somewhere/else.html">Not a PDF</a>
 <a href="/assets/pdfs/TaxSale99-PropertyList.pdf">duplicate link</a>
</body></html>
"""

LIST_TEXT = """KNOX COUNTY TRUSTEE
DELINQUENT TAX SALE NO. 99
The sale will be held on March 4, 2027 at 10:00 AM
Delinquent years 2021 through 2024
1  081JA018   412 SAMPLE HOLLOW RD QUILLFEATHER MARLOWE
2  094KB-007  88 INVENTED WAY THORNBURY ESME
3  123AB 004.01  0 NOWHERE LN HOLLOWAY LAND TRUST
4  057-012   17 MADE UP CT ASHGROVE PERPETUA
5  071EA021   3 FICTION DR UNIT B WREN ODALYS
6  099ZZ999   55 GHOST PKWY NOBODY
call 865-555-0100 with questions
"""

ROLL = {
    "081JA018": {"address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE"},
    "094KB007": {"address": "88 INVENTED WAY", "owner": "THORNBURY ESME"},
    "123AB004.01": {"address": "0 NOWHERE LN", "owner": "HOLLOWAY LAND TRUST"},
    "057-012": {"address": "17 MADE UP CT", "owner": "ASHGROVE PERPETUA"},
    "071EA021": {"address": "3 FICTION DR", "owner": "WREN ODALYS"},
}


class Web:
    def __init__(self, html=HTML, files=None, fail=()):
        self.html, self.fail, self.asked = html, set(fail), []
        self.files = files or {"PropertyList": LIST_PDF, "FINAL_RESULTS": RESULT_PDF, "Terms": b"%PDF-1.4 terms"}

    def __call__(self, url, *a, **k):
        self.asked.append(url)
        if url in self.fail or any(f in url for f in self.fail):
            raise RuntimeError("403")
        if url == PAGE:
            return self.html.encode("utf-8")
        for key, body in self.files.items():
            if key in url:
                return body
        raise RuntimeError("404")


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    monkeypatch.setenv("KNOX_TAX_SALE_PAGE", PAGE)
    for name in ("KNOX_TAX_SALE_MAX_PARCELS", "KNOX_TAX_SALE_DATE", "KNOX_TAX_SALE_RECHECK_DAYS",
                 "CODE_VIOLATION_STALE_DAYS", "CODE_VIOLATION_TYPES", "CODE_VIOLATION_STATUSES",
                 "CODE_VIOLATION_MAX_AGE_DAYS"):
        monkeypatch.delenv(name, raising=False)


def prepare(tmp_path, web=None, today="2026-11-13", roll=None, text=LIST_TEXT):
    asked = []

    def lookup(pin):
        asked.append(pin)
        return (roll if roll is not None else ROLL).get(pin)

    stats = T.prepare(str(tmp_path), http_get=web or Web(), pdf_text=lambda path: text,
                      parcel_lookup=lookup, today=today)
    stats["_asked"] = asked
    return stats


# ── reading the page ──────────────────────────────────────────────────


def test_every_pdf_link_is_read_once_with_its_words():
    links = T.links_from_page(HTML, PAGE)
    assert [x["label"] for x in links] == ["Tax Sale 99 Property List", "Tax Sale 98 Final Results",
                                           "Terms and Conditions"]
    assert links[0]["url"] == "https://trustee.example.invalid/assets/pdfs/TaxSale99-PropertyList.pdf"
    assert links[1]["url"].endswith("/services/assets/pdfs/TaxSale98-FINAL_RESULTS.pdf")


def test_a_document_is_sorted_by_what_its_link_says():
    assert T.classify("Tax Sale 99 Property List", "x.pdf") == "list"
    assert T.classify("Final Results", "TaxSale98-FINAL_RESULTS.pdf") == "results"
    assert T.classify("Report of Sale", "a.pdf") == "results"
    assert T.classify("Decree Confirming Sale", "a.pdf") == "results"
    assert T.classify("Terms and Conditions", "a.pdf") == "other"
    assert T.classify("Bidder registration list", "a.pdf") == "other", "a list of bidders is not a property list"


def test_parcel_ids_are_read_in_order_each_once():
    assert T.parcel_ids(LIST_TEXT) == ["081JA018", "094KB007", "123AB004.01", "057-012", "071EA021", "099ZZ999"]
    assert T.parcel_ids("call 865-555-0100 or 865 555 0100") == [], "a phone number is not a parcel"
    assert T.parcel_ids("081JA018 and again 081JA018") == ["081JA018"]


def test_the_sale_date_is_the_date_next_to_the_word_sale():
    assert T.sale_date(LIST_TEXT) == "2027-03-04"
    assert T.sale_date("Recorded 01/05/2019. Taxes for 2021.") == "", "a date with no sale near it is not the sale date"
    assert T.sale_date("", "Tax Sale 12/02/2026 property list") == "2026-12-02"
    assert T.sale_date("Sale on February 30, 2027") == ""


# ── capture ───────────────────────────────────────────────────────────


def test_every_new_document_is_archived_and_named(tmp_path):
    got = T.capture(str(tmp_path), Web(), "2026-11-13")
    assert [d["label"] for d in got["new"]] == ["Tax Sale 99 Property List", "Tax Sale 98 Final Results",
                                                "Terms and Conditions"]
    assert got["problems"] == []
    assert len(os.listdir(tmp_path / "tax_sale_archive")) == 3
    assert {d["kind"] for d in got["docs"].values()} == {"list", "results", "other"}


def test_a_known_link_is_not_downloaded_again_the_next_morning(tmp_path):
    T.capture(str(tmp_path), Web(), "2026-11-13")
    web = Web()
    got = T.capture(str(tmp_path), web, "2026-11-14")
    assert got["new"] == [] and web.asked == [PAGE]


def test_a_link_reused_for_a_new_file_is_kept_as_a_second_document(tmp_path):
    T.capture(str(tmp_path), Web(), "2026-11-13")
    changed = Web(files={"PropertyList": b"%PDF-1.4 list, revised", "FINAL_RESULTS": RESULT_PDF, "Terms": b"%PDF-1.4 terms"})
    got = T.capture(str(tmp_path), changed, "2026-11-21")            # past the recheck window
    assert [d["label"] for d in got["new"]] == ["Tax Sale 99 Property List"]
    assert len(got["docs"]) == 4


def test_a_page_that_cannot_be_read_is_a_problem(tmp_path):
    got = T.capture(str(tmp_path), Web(fail=[PAGE]), "2026-11-13")
    assert got["new"] == [] and "could not be read" in got["problems"][0]


def test_a_page_with_no_pdf_link_is_a_problem_not_a_quiet_day(tmp_path):
    got = T.capture(str(tmp_path), Web(html="<html>moved</html>"), "2026-11-13")
    assert "links no PDF at all" in got["problems"][0]


def test_a_link_that_does_not_return_a_pdf_is_a_problem(tmp_path):
    web = Web(files={"PropertyList": b"<html>login</html>", "FINAL_RESULTS": RESULT_PDF, "Terms": b"%PDF-1.4"})
    got = T.capture(str(tmp_path), web, "2026-11-13")
    assert any("is not a PDF" in p for p in got["problems"])


# ── staging ───────────────────────────────────────────────────────────


def test_each_parcel_is_resolved_on_the_roll_and_staged_with_the_sale_date(tmp_path):
    stats = prepare(tmp_path)
    rows = ftm_staging.pending_rows(str(tmp_path), T.FEED)
    assert stats["staged"] == 4 and len(rows) == 4
    assert stats["no_house_number"] == 1 and stats["not_on_roll"] == 1
    r = rows[0]
    assert r["address"] == "412 Sample Hollow Rd" and r["owner_raw"] == "Quillfeather Marlowe"
    assert r["sale_date"] == "2027-03-04" and r["notice_type"] == "tax_sale" and r["feed"] == "knox_tax_sale_report"
    assert r["tax_account"] == "081JA018" and r["filed_date"] == "2026-11-13"
    assert r["source_url"].endswith("TaxSale99-PropertyList.pdf")


def test_the_results_document_is_announced_but_never_staged(tmp_path):
    stats = prepare(tmp_path)
    assert "Tax Sale 98 Final Results" in stats["new_documents"]
    assert stats["lists"] == 1, "only the property list is read for parcels"
    assert all(r["source_url"].endswith("PropertyList.pdf") for r in ftm_staging.pending_rows(str(tmp_path), T.FEED))


def test_a_list_whose_sale_has_been_held_is_skipped(tmp_path):
    stats = prepare(tmp_path, today="2027-03-10")
    assert stats["staged"] == 0 and stats["sale_held"] == 1
    assert T.describe(stats).startswith("3 new document(s)")


def test_a_list_with_no_readable_sale_date_is_staged_and_flagged(tmp_path):
    stats = prepare(tmp_path, text=LIST_TEXT.replace("The sale will be held on March 4, 2027 at 10:00 AM", ""))
    assert stats["staged"] == 4 and stats["no_sale_date"] == 1
    assert ftm_staging.pending_rows(str(tmp_path), T.FEED)[0]["sale_date"] == ""
    assert "sale date could not be read" in T.describe(stats)


def test_the_sale_date_can_be_supplied_when_the_document_hides_it(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOX_TAX_SALE_DATE", "2027-04-01")
    prepare(tmp_path, text=LIST_TEXT.replace("The sale will be held on March 4, 2027 at 10:00 AM", ""))
    assert ftm_staging.pending_rows(str(tmp_path), T.FEED)[0]["sale_date"] == "2027-04-01"


def test_a_list_that_yields_almost_no_parcels_fails_loudly(tmp_path):
    stats = prepare(tmp_path, text="scanned image, no text layer 081JA018")
    assert stats["staged"] == 0
    assert any("only 1 parcel id(s)" in p for p in stats["problems"])


def test_a_parcel_is_looked_up_once_and_never_again_after_the_upload(tmp_path):
    first = prepare(tmp_path)
    assert len(first["_asked"]) == 6
    ftm_staging.mark_uploaded(str(tmp_path), T.FEED)
    second = prepare(tmp_path, today="2026-11-14")
    assert second["_asked"] == [] and second["staged"] == 0


def test_the_days_cap_leaves_the_rest_for_tomorrow(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOX_TAX_SALE_MAX_PARCELS", "2")
    stats = prepare(tmp_path)
    assert stats["lookups"] == 2 and stats["left_for_tomorrow"] == 4
    ftm_staging.mark_uploaded(str(tmp_path), T.FEED)
    nxt = prepare(tmp_path, today="2026-11-14")
    assert nxt["lookups"] == 2 and nxt["_asked"] == ["123AB004.01", "057-012"]


def test_a_lookup_that_errors_is_left_open(tmp_path):
    def boom(pin):
        raise RuntimeError("timeout")

    stats = T.prepare(str(tmp_path), http_get=Web(), pdf_text=lambda p: LIST_TEXT, parcel_lookup=boom, today="2026-11-13")
    assert stats["staged"] == 0 and stats["left_for_tomorrow"] == 6
    assert ftm_staging.pending(str(tmp_path), T.FEED)["keys"] == []


def test_with_no_open_list_the_report_says_so(tmp_path):
    T.capture(str(tmp_path), Web(), "2026-11-13")
    stats = prepare(tmp_path, today="2027-06-01")
    assert T.describe(stats) == "no open tax sale list on the Trustee's page (1 past sale(s) skipped)"


# ── the live page's shape (read 2026-11-20): site forms, the announced date, the dash ──

EN = chr(0x2013)
ANNOUNCED = """
<html><head><script>var d = "Tax Sale 97 on January 5, 2020";</script></head><body>
 <a href="https://property.example.invalid/assets/pdfs/TangiblePersonalPropertySchedule.pdf">Tangible Personal Property</a>
 <a href="https://property.example.invalid/assets/pdfs/mailing_change.pdf">Mailing Address Change Form</a>
 <p>Important Tax Sale 99 Information: Event Details: June 2nd, 2026 - Doors Open: 8:00am</p>
 <a href="/assets/pdfs/TaxSale99-FINAL_RESULTS_PDF.pdf">Tax Sale 99 %s Final Results</a>
 <a href="/assets/pdfs/TaxSale99-DecreeConfirmingSale.pdf">Tax Sale 99 %s Decree Confirming Sale</a>
 <a href="/assets/pdfs/TaxSale98-FINAL_RESULTS.pdf">Tax Sale 98 Final Results</a>
 <p>Tax Sale dates are determined by court proceedings and will be listed accordingly.</p>
</body></html>
""" % (EN, EN)


def announced_web(html=ANNOUNCED):
    return Web(html=html, files={"TangiblePersonal": b"%PDF-1.4 form", "mailing_change": b"%PDF-1.4 mail form",
                                 "TaxSale99-FINAL": b"%PDF-1.4 results 99", "Decree": b"%PDF-1.4 decree 99",
                                 "TaxSale98-FINAL": b"%PDF-1.4 results 98"})


def test_the_trustees_site_forms_are_never_a_property_list():
    assert T.classify("Tangible Personal Property", "x/TangiblePersonalPropertySchedule.pdf") == "other"
    assert T.classify("Mailing Address Change Form", "x/mailing_change.pdf") == "other"
    assert T.site_form("Tangible Personal Property", "x/TangiblePersonalPropertySchedule.pdf")
    assert T.site_form("Mailing Address Change Form", "x/mailing_change.pdf")
    assert not T.site_form("Tax Sale 25 - Final Results", "x/TaxSale25-FiNAL_RESULTS_PDF.pdf")
    assert T.classify("Tax Sale 99 Property List", "x/TaxSale99-PropertyList.pdf") == "list", "a real list still is one"
    assert T.classify("Delinquent Properties", "x/delinquent.pdf") == "list"
    assert T.classify("Property Owner Guide", "x/guide.pdf") == "other", "a list must say it is for a sale"


def test_a_form_an_older_run_filed_as_a_list_no_longer_fails_the_feed(tmp_path):
    # The first-to-market box's archive, as the code before 2026-11-20 wrote it: the Tangible Personal
    # Property form filed as a property list, which then read 0 parcel ids and failed the stage.
    arc = tmp_path / "tax_sale_archive"
    arc.mkdir()
    (arc / "f0f0f0f0f0f0.pdf").write_bytes(b"%PDF-1.4 form")
    old = {"f0f0f0f0f0f0": {"url": "https://property.example.invalid/assets/pdfs/TangiblePersonalPropertySchedule.pdf",
                            "label": "Tangible Personal Property", "kind": "list", "first_seen": "2026-11-20",
                            "checked": "2026-11-20", "path": str(arc / "f0f0f0f0f0f0.pdf")}}
    (tmp_path / T.DOCS).write_text(__import__("json").dumps(old), encoding="utf-8")
    stats = prepare(tmp_path, web=announced_web(), today="2026-11-20", text="a form with no parcel in it")
    assert stats["problems"] == [] and stats["lists"] == 0, stats["problems"]
    assert T._load_docs(str(tmp_path))["f0f0f0f0f0f0"]["kind"] == "other", "the archive is filed again by today's rules"
    down = prepare(tmp_path, web=Web(fail=[PAGE]), today="2026-11-21", text="a form with no parcel in it")
    assert not any("parcel id" in p for p in down["problems"]), "on a day the page cannot be read the old filing still does not count"


def test_a_page_holding_only_the_sites_forms_has_lost_its_tax_sale_documents(tmp_path):
    only_forms = '<a href="https://property.example.invalid/TangiblePersonalPropertySchedule.pdf">Tangible Personal Property</a>'
    got = T.capture(str(tmp_path), Web(html=only_forms), "2026-11-20")
    assert got["new"] == [] and "links only the site's own forms" in got["problems"][0]


def test_a_documents_sale_number_comes_from_its_label_or_its_file_name():
    assert T.sale_number("Tax Sale 25 - Final Results") == "25"
    assert T.sale_number("Final Results", "https://x/assets/pdfs/TaxSale25-FiNAL_RESULTS_PDF.pdf") == "25"
    assert T.sale_number("Tax Sale No. 007 report") == "7"
    assert T.sale_number("Mailing Address Change Form", "x/mailing_change.pdf") == ""


def test_the_page_announces_each_sales_date():
    assert T.page_sales(ANNOUNCED) == {"99": "2026-06-02"}, "a script's text is not the page, and a sale named with no date is left out"
    assert T.page_sales("<p>Tax Sale 98 results were posted on the court's site.</p>") == {}


def test_capture_keeps_the_pages_date_on_each_document_of_that_sale(tmp_path):
    web = announced_web()
    got = T.capture(str(tmp_path), web, "2026-11-20")
    by_label = {d["label"]: d for d in got["docs"].values()}
    assert by_label["Tax Sale 99 - Final Results"]["sale_date_page"] == "2026-06-02"
    assert by_label["Tax Sale 99 - Decree Confirming Sale"]["sale_date_page"] == "2026-06-02"
    assert "sale_date_page" not in by_label["Tax Sale 98 Final Results"], "the page announced no date for sale 98"
    assert "Tangible Personal Property" not in by_label and not any("TangiblePersonal" in u or "mailing_change" in u
                                                                      for u in web.asked), "the site's forms are never downloaded"
    assert all(d.get("sale_date_page") == "2026-06-02" for d in got["new"] if "99" in d["label"])
    # The page moves on to the next sale: the documents of sale 99 keep the date they were given.
    later = ANNOUNCED.replace("Tax Sale 99 Information: Event Details: June 2nd, 2026",
                              "Tax Sale 100 Information: Event Details: June 1st, 2027")
    again = T.capture(str(tmp_path), announced_web(later), "2026-12-30")
    assert {d["label"]: d.get("sale_date_page") for d in again["docs"].values()}["Tax Sale 99 - Final Results"] == "2026-06-02"


def test_a_label_is_kept_with_plain_hyphens_whatever_dash_the_page_used(tmp_path):
    got = T.capture(str(tmp_path), announced_web(), "2026-11-20")
    labels = [d["label"] for d in got["docs"].values()]
    assert "Tax Sale 99 - Final Results" in labels and not any(EN in x or chr(0x2014) in x for x in labels)
    assert T.links_from_page('<a href="a.pdf">Fees &amp; Costs</a>', PAGE)[0]["label"] == "Fees & Costs"


def test_the_page_address_can_be_given_to_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOX_TAX_SALE_PAGE", "https://somewhere.example.invalid/not-this-one")
    web = Web()
    got = T.capture(str(tmp_path), web, "2026-11-13", page=PAGE)
    assert web.asked[0] == PAGE and got["problems"] == []


# ── code violations ───────────────────────────────────────────────────

EXPORT = """Record Number,Record Type,Status,Opened Date,Address,Parcel,Description
CE-26-0001,Structure Complaint,Open,11/10/2026,"412 Sample Hollow Rd, Knoxville TN 37918",081JA018,roof collapsed
BP-26-0002,Residential Building Permit,Issued,11/09/2026,"88 Invented Way, Knoxville TN 37917",094KB007,new deck
BP-25-0003,Residential Building Permit,Expired,10/20/2026,"17 Made Up Ct, Knoxville TN 37917",057012,rehab abandoned
CE-26-0004,Dirty Lot,Lien Owed - Closed,11/01/2026,"3 Fiction Dr, Knoxville TN 37917",071EA021,
CE-26-0005,Structure Complaint,Open,11/08/2026,Corner of Main and Elm,,
CE-24-0006,Structure Complaint,Open,02/01/2024,"9 Stale Ave, Knoxville TN 37917",,old case
"""


def drop(tmp_path, name="export.csv", content=EXPORT):
    box = tmp_path / "inbox" / "code_violations"
    box.mkdir(parents=True, exist_ok=True)
    (box / name).write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def test_only_distress_records_are_staged(tmp_path):
    drop(tmp_path)
    stats = C.prepare(str(tmp_path), today="2026-11-13")
    rows = ftm_staging.pending_rows(str(tmp_path), C.FEED)
    assert [r["case_number"] for r in rows] == ["CE-26-0001", "BP-25-0003", "CE-26-0004"]
    assert stats["staged"] == 3 and stats["routine"] == 1 and stats["no_address"] == 1 and stats["too_old"] == 1
    assert stats["problems"] == []


def test_an_expired_permit_and_a_city_lien_are_kept_whatever_their_type(tmp_path):
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")
    by_case = {r["case_number"]: r for r in ftm_staging.pending_rows(str(tmp_path), C.FEED)}
    assert "status Expired" in by_case["BP-25-0003"]["notes"]
    assert "status Lien Owed - Closed" in by_case["CE-26-0004"]["notes"]


def test_a_staged_row_carries_the_street_line_the_parcel_and_its_own_list(tmp_path):
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")
    r = ftm_staging.pending_rows(str(tmp_path), C.FEED)[0]
    assert r["address"] == "412 Sample Hollow Rd" and r["tax_account"] == "081JA018"
    assert r["filed_date"] == "2026-11-10" and r["feed"] == "knox_code_violations"
    assert r["notice_type"] == "code_violation" and "_key" not in r


def test_the_same_export_dropped_twice_stages_nothing_new(tmp_path):
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")
    ftm_staging.mark_uploaded(str(tmp_path), C.FEED)
    drop(tmp_path, "export_copy.csv")
    stats = C.prepare(str(tmp_path), today="2026-11-14")
    assert stats["staged"] == 0 and stats["already_uploaded"] == 6 and stats["new_files"] == 0


def test_a_row_whose_upload_failed_is_staged_again_without_a_second_drop(tmp_path):
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")                 # staged, county stage never ran
    stats = C.prepare(str(tmp_path), today="2026-11-14")
    assert stats["staged"] == 3


def test_nothing_in_the_inbox_is_moved_or_deleted(tmp_path):
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")
    assert os.listdir(tmp_path / "inbox" / "code_violations") == ["export.csv"]


def test_the_citys_html_table_export_is_read_too(tmp_path):
    html = ("<html><table><tr><th>Record Number</th><th>Record Type</th><th>Status</th><th>Opened Date</th>"
            "<th>Address</th></tr><tr><td>CE-26-0100</td><td>Dirty Lot</td><td>Open</td><td>11/11/2026</td>"
            "<td>21 Table Row Ln</td></tr></table></html>")
    drop(tmp_path, "export.xls", html)
    stats = C.prepare(str(tmp_path), today="2026-11-13")
    assert stats["staged"] == 1
    assert ftm_staging.pending_rows(str(tmp_path), C.FEED)[0]["address"] == "21 Table Row Ln"


def test_a_file_with_no_address_column_fails_and_names_its_columns(tmp_path):
    drop(tmp_path, "wrong.csv", "Permit,Contractor,Fee\nBP-1,Acme,100\n")
    stats = C.prepare(str(tmp_path), today="2026-11-13")
    assert stats["staged"] == 0
    assert "wrong.csv could not be used: no address column" in stats["problems"][0]
    assert "Permit, Contractor, Fee" in stats["problems"][0]


def test_a_real_excel_workbook_is_refused_with_what_to_do(tmp_path):
    drop(tmp_path, "book.xls", b"PK\x03\x04" + b"\x00" * 40)
    stats = C.prepare(str(tmp_path), today="2026-11-13")
    assert "Save it as CSV" in stats["problems"][0]


def test_a_list_that_stops_arriving_fails_by_name(tmp_path):
    drop(tmp_path)
    assert C.prepare(str(tmp_path), today="2026-11-13")["problems"] == []
    assert C.prepare(str(tmp_path), today="2026-11-20")["problems"] == [], "a week is fine"
    late = C.prepare(str(tmp_path), today="2026-11-30")
    assert "no new code violation file for 17 days" in late["problems"][0]


def test_an_empty_inbox_fails_until_the_first_file_arrives(tmp_path):
    stats = C.prepare(str(tmp_path), today="2026-11-13")
    assert "no code violation file has ever been dropped" in stats["problems"][0]
    assert C.describe(stats) == "no file in the code violation inbox"


def test_the_stale_alarm_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_VIOLATION_STALE_DAYS", "0")
    assert C.prepare(str(tmp_path), today="2026-11-13")["problems"] == []


def test_the_distress_types_are_a_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_VIOLATION_TYPES", "residential building permit")
    monkeypatch.setenv("CODE_VIOLATION_STATUSES", "nothing")
    drop(tmp_path)
    C.prepare(str(tmp_path), today="2026-11-13")
    assert [r["case_number"] for r in ftm_staging.pending_rows(str(tmp_path), C.FEED)] == ["BP-26-0002", "BP-25-0003"]


def test_the_modules_hold_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/knox_tax_sale.py", "src/code_violations.py", "tests/test_knox_tax_sale.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
