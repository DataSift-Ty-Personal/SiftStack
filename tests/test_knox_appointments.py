"""knox_appointments: substitute trustee appointments, followed to the borrower's house.

Zero network. Every recorder row, name, parcel and instrument number below is invented. A fake
stands in for the recorder (a doc type and a date window in, grid rows out), for the county's
parcel lookup and for the name search.

The rule under test more than any other: the appointment's index names the LENDER and the
TRUSTEE, and neither of them may ever be matched to a house.

Run: python -m pytest tests/test_knox_appointments.py
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import ftm_staging  # noqa: E402
import knox_appointments as A  # noqa: E402
import knox_rod  # noqa: E402

APPT_1 = "202611040000111"       # recorded 2026-11-04
APPT_2 = "202611050000222"
APPT_3 = "202611060000333"
DEED_1 = "201903150000444"       # a deed of trust recorded 2019-03-15
DEED_2 = "202007010000555"
DEED_3 = "201803020000666"

LENDER = "FIRST EXAMPLE BANK"
TRUSTEE = "HOLLIS AND PIKE LAW FIRM"


def row(instrument, name, role, recorded="11/04/2026", legal="", marginal="", amount="", doc="APPT"):
    """One raw grid row, 16 columns, in the layout knox_rod documents."""
    r = [""] * 16
    r[3] = "Recorded: %s <br> Book 0" % recorded
    r[4], r[6], r[8], r[9], r[10], r[13], r[14] = name, role, doc, legal, instrument, marginal, amount
    return r


def appointment(instrument, deed="", recorded="11/04/2026"):
    return [row(instrument, LENDER, "Grantor", recorded, marginal=deed),
            row(instrument, TRUSTEE, "Grantee", recorded, marginal=deed)]


def deed_of_trust(instrument, borrowers, recorded="03/15/2019", legal="", amount="$185,000.00"):
    rows = [row(instrument, b, "Grantor", recorded, legal=legal, amount=amount, doc="TD") for b in borrowers]
    rows.append(row(instrument, LENDER, "Grantee", recorded, legal=legal, amount=amount, doc="TD"))
    return rows


class Recorder:
    """Answers fetch_rows(label, d_from, d_to) from a script and remembers what was asked."""

    def __init__(self, appointments, deeds_by_day=None):
        self.appointments, self.deeds_by_day, self.asked = appointments, deeds_by_day or {}, []

    def __call__(self, label, d_from, d_to):
        self.asked.append((label, d_from, d_to))
        if label == "APPT":
            return list(self.appointments)
        return list(self.deeds_by_day.get(d_from, []))


class Lookups:
    def __init__(self, parcels=None, by_name=None, fail=()):
        self.parcels, self.by_name, self.fail = parcels or {}, by_name or {}, set(fail)
        self.parcel_asked, self.name_asked = [], []

    def parcel(self, pin):
        self.parcel_asked.append(pin)
        return self.parcels.get(pin)

    def name(self, rec):
        self.name_asked.append(rec["name"])
        if rec["name"] in self.fail:
            return dict(rec, error="URLError: timed out", n_parcels=0)
        hits = self.by_name.get(rec["name"], [])
        return dict(rec, n_parcels=len(hits), parcels=hits, institutional=len(hits) > 15)


TODAY = date(2026, 11, 13)


def run(tmp_path, recorder, lookups, today=TODAY, months=1):
    return A.prepare(str(tmp_path), months=months, fetch_rows=recorder, parcel_lookup=lookups.parcel,
                     name_resolver=lookups.name, sleep=lambda s: None, today=today)


# ── reading the index ─────────────────────────────────────────────────


def test_the_two_sides_of_an_instrument_are_kept_apart():
    got = A._parties(appointment(APPT_1, DEED_1))[APPT_1]
    assert got["grantors"] == [LENDER] and got["grantees"] == [TRUSTEE]
    assert got["refs"] == [DEED_1] and got["recorded"] == "2026-11-04"


def test_an_instrument_does_not_refer_to_itself():
    rows = [row(APPT_1, LENDER, "Grantor", marginal="%s %s" % (APPT_1, DEED_1))]
    assert A._parties(rows)[APPT_1]["refs"] == [DEED_1]


def test_an_instrument_number_carries_its_recording_day():
    assert A.instrument_day(DEED_1) == "03/15/2019"
    assert A.instrument_day("201913450000444") == "", "month 13 is not a day"
    assert A.instrument_day("BOOK 1234 PAGE 56") == ""
    assert A.instrument_day("") == ""


def test_lender_and_trustee_shapes_are_recognised():
    for name in (LENDER, TRUSTEE, "EXAMPLE MORTGAGE SERVICING LLC", "MERS INC", "RIVERBEND CREDIT UNION",
                 "DOE PAT TRUSTEE", "SMITH AND JONES PLLC", "SECRETARY OF HOUSING"):
        assert A.looks_like_lender(name), name


def test_a_person_whose_surname_sounds_like_a_firm_is_still_a_person():
    for name in ("LAW MARGARET ANN", "BANKS TOBIAS", "NA SUNG", "TITUS FEDERICO"):
        assert not A.looks_like_lender(name), name


def test_only_people_on_the_grantor_side_of_the_deed_of_trust_are_borrowers():
    deed = A._parties(deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE", "QUILLFEATHER", LENDER]))[DEED_1]
    assert A.borrowers_of(deed) == ["QUILLFEATHER MARLOWE"], "a bare surname and a bank are both dropped"


def test_a_name_matches_an_owner_in_any_order_and_never_on_one_token():
    assert A.name_in_owner("QUILLFEATHER MARLOWE J", "MARLOWE QUILLFEATHER & ADA QUILLFEATHER")
    assert not A.name_in_owner("QUILLFEATHER MARLOWE", "QUILLFEATHER ADA")
    assert not A.name_in_owner("QUILLFEATHER", "QUILLFEATHER MARLOWE")


# ── the whole pass ────────────────────────────────────────────────────


def test_an_appointment_is_followed_to_the_borrowers_house_by_parcel_id(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1),
                   {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"], legal="LOT 4 PrpId:081JA018")})
    look = Lookups(parcels={"081JA018": {"address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE"}})
    stats = run(tmp_path, rec, look)
    assert stats["new"] == 1 and stats["staged"] == 1
    staged = ftm_staging.pending_rows(str(tmp_path), A.FEED)
    assert len(staged) == 1
    r = staged[0]
    assert r["address"] == "412 SAMPLE HOLLOW RD" and r["notice_type"] == "substitute_trustee"
    assert r["case_number"] == APPT_1 and r["filed_date"] == "2026-11-04"
    assert r["tax_account"] == "081JA018" and r["loan_amount"] == 185000.0
    assert "QUILLFEATHER MARLOWE" in r["notes"] and DEED_1 in r["notes"] and "$185,000.00" in r["notes"]


def test_the_lender_and_the_trustee_are_never_looked_up(tmp_path):
    """The index names only them. Matching either one finds a bank's or an attorney's property."""
    rec = Recorder(appointment(APPT_1, DEED_1),
                   {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    look = Lookups(by_name={LENDER: [{"account": "1", "owner": LENDER, "address": "9 BANK PLAZA"}],
                            TRUSTEE: [{"account": "2", "owner": TRUSTEE, "address": "7 ATTORNEY ROW"}]})
    stats = run(tmp_path, rec, look)
    assert LENDER not in look.name_asked and TRUSTEE not in look.name_asked
    assert look.name_asked == ["QUILLFEATHER MARLOWE"]
    assert stats["staged"] == 0
    assert all("BANK PLAZA" not in r["address"] and "ATTORNEY ROW" not in r["address"]
               for r in ftm_staging.pending_rows(str(tmp_path), A.FEED))


def test_a_deed_of_trust_whose_only_grantor_is_a_bank_yields_no_borrower(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, [LENDER])})
    look = Lookups(by_name={LENDER: [{"account": "1", "owner": LENDER, "address": "9 BANK PLAZA"}]})
    stats = run(tmp_path, rec, look)
    assert stats["staged"] == 0 and look.name_asked == []
    assert stats["outcomes"] == {"the deed of trust names no borrower": 1}


def test_a_house_that_changed_hands_since_the_deed_of_trust_is_not_a_lead(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1),
                   {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"], legal="PrpId:081JA018")})
    look = Lookups(parcels={"081JA018": {"address": "412 SAMPLE HOLLOW RD", "owner": "THORNBURY ESME"}})
    stats = run(tmp_path, rec, look)
    assert stats["staged"] == 0
    assert stats["outcomes"] == {"the house has a different owner today": 1}
    assert not ftm_staging.pending_rows(str(tmp_path), A.FEED)


def test_a_borrower_with_one_parcel_is_matched_by_name(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    look = Lookups(by_name={"QUILLFEATHER MARLOWE": [
        {"account": "094KB-007", "owner": "QUILLFEATHER MARLOWE & ADA", "address": "88 INVENTED WAY"}]})
    stats = run(tmp_path, rec, look)
    assert stats["staged"] == 1
    r = ftm_staging.pending_rows(str(tmp_path), A.FEED)[0]
    assert r["address"] == "88 INVENTED WAY" and r["tax_account"] == "094KB-007"
    assert "name on the tax roll" in r["notes"]


def test_a_borrower_with_two_parcels_is_ambiguous_and_nothing_is_uploaded(tmp_path):
    """A lien attaches to everything its debtor owns. A deed of trust secures ONE house."""
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    look = Lookups(by_name={"QUILLFEATHER MARLOWE": [
        {"account": "A1", "owner": "QUILLFEATHER MARLOWE", "address": "88 INVENTED WAY"},
        {"account": "A2", "owner": "QUILLFEATHER MARLOWE", "address": "90 INVENTED WAY"}]})
    stats = run(tmp_path, rec, look)
    assert stats["staged"] == 0
    assert list(stats["outcomes"]) == ["the borrower owns 2 parcels and the deed of trust does not say which"]


def test_a_name_search_row_owned_by_someone_else_is_not_a_match(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    look = Lookups(by_name={"QUILLFEATHER MARLOWE": [
        {"account": "A9", "owner": "QUILLFEATHER ADA", "address": "5 NAMESAKE CT"}]})
    stats = run(tmp_path, rec, look)
    assert stats["staged"] == 0
    assert stats["outcomes"] == {"no parcel on the tax roll in the borrower's name": 1}


def test_a_borrower_holding_many_parcels_is_an_institution(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["OAKMERE HOLDINGS LLC"])})
    many = [{"account": "P%d" % i, "owner": "OAKMERE HOLDINGS LLC", "address": "%d ROW ST" % i} for i in range(16)]
    stats = run(tmp_path, rec, Lookups(by_name={"OAKMERE HOLDINGS LLC": many}))
    assert stats["staged"] == 0
    assert stats["outcomes"] == {"the borrower holds too many parcels to be a homeowner": 1}


def test_a_miss_is_settled_once_the_upload_runs_and_is_not_looked_up_again(tmp_path):
    rec = Recorder(appointment(APPT_1, ""))                       # names no deed of trust
    stats = run(tmp_path, rec, Lookups())
    assert stats["settled"] == 1 and stats["staged"] == 0
    assert stats["outcomes"] == {"names no deed of trust the index can follow": 1}
    assert ftm_staging.is_new(str(tmp_path), A.FEED, APPT_1), "not handled until the county stage has run"
    ftm_staging.mark_uploaded(str(tmp_path), A.FEED)
    again = Recorder(appointment(APPT_1, ""))
    stats = run(tmp_path, again, Lookups())
    assert stats["new"] == 0 and A.describe(stats).startswith("no new substitute trustee appointments")


def test_a_lookup_that_errors_stays_open_for_tomorrow(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    stats = run(tmp_path, rec, Lookups(fail=["QUILLFEATHER MARLOWE"]))
    assert stats["settled"] == 0 and stats["staged"] == 0
    assert stats["outcomes"] == {"left for tomorrow (the tax roll lookup failed)": 1}
    ftm_staging.mark_uploaded(str(tmp_path), A.FEED)
    assert ftm_staging.is_new(str(tmp_path), A.FEED, APPT_1)


def test_the_deed_of_trust_is_fetched_for_one_day_under_its_own_label(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1), {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"])})
    run(tmp_path, rec, Lookups())
    deed_asks = [a for a in rec.asked if a[0] != "APPT"]
    assert deed_asks == [("TD", "03/15/2019", "03/15/2019")]


def test_two_appointments_on_one_recording_day_cost_one_request(tmp_path):
    other = "201903150000777"
    rows = appointment(APPT_1, DEED_1) + appointment(APPT_2, other, "11/05/2026")
    deeds = deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"]) + deed_of_trust(other, ["THORNBURY ESME"])
    rec = Recorder(rows, {"03/15/2019": deeds})
    stats = run(tmp_path, rec, Lookups())
    assert stats["lookups"] == 1 and len([a for a in rec.asked if a[0] == "TD"]) == 1


def test_the_days_request_cap_defers_the_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOX_APPT_MAX_LOOKUPS", "1")
    rows = appointment(APPT_1, DEED_1) + appointment(APPT_2, DEED_2, "11/05/2026")
    rec = Recorder(rows, {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"]),
                          "07/01/2020": deed_of_trust(DEED_2, ["THORNBURY ESME"], recorded="07/01/2020")})
    stats = run(tmp_path, rec, Lookups())
    assert stats["lookups"] == 1
    assert stats["outcomes"].get("left for tomorrow (the day's lookup cap was reached)") == 1
    ftm_staging.mark_uploaded(str(tmp_path), A.FEED)
    assert ftm_staging.is_new(str(tmp_path), A.FEED, APPT_2), "a deferred appointment is not settled"


# ── a feed that cannot work must say so ───────────────────────────────


def test_several_appointments_with_no_deed_of_trust_reference_fail_the_stage(tmp_path):
    rows = appointment(APPT_1, "") + appointment(APPT_2, "", "11/05/2026") + appointment(APPT_3, "", "11/06/2026")
    with pytest.raises(A.AppointmentsUnusable) as exc:
        run(tmp_path, Recorder(rows), Lookups())
    assert "3 substitute trustee appointments" in str(exc.value) and "no borrower" in str(exc.value)
    assert not ftm_staging.pending(str(tmp_path), A.FEED)["keys"], "nothing is settled when the feed cannot run"


def test_one_or_two_such_appointments_are_just_misses(tmp_path):
    stats = run(tmp_path, Recorder(appointment(APPT_1, "") + appointment(APPT_2, "", "11/05/2026")), Lookups())
    assert stats["settled"] == 2


def test_deeds_of_trust_that_are_never_found_fail_the_stage_and_name_the_label(tmp_path):
    rows = (appointment(APPT_1, DEED_1) + appointment(APPT_2, DEED_2, "11/05/2026")
            + appointment(APPT_3, DEED_3, "11/06/2026"))
    with pytest.raises(A.AppointmentsUnusable) as exc:
        run(tmp_path, Recorder(rows, {}), Lookups())
    assert "KNOX_ROD_DOT_LABEL" in str(exc.value) and "'TD'" in str(exc.value)


def test_a_long_window_with_no_appointment_at_all_fails_the_stage(tmp_path):
    """Nothing else can tell "none was recorded" from "the search read nothing"."""
    with pytest.raises(A.AppointmentsUnusable) as exc:
        run(tmp_path, Recorder([]), Lookups())                    # the 13th: thirteen days covered
    said = str(exc.value)
    assert "13 days" in said and "KNOX_ROD_APPT_LABEL" in said and "'APPT'" in said


def test_a_short_window_with_none_is_a_quiet_week(tmp_path):
    stats = run(tmp_path, Recorder([]), Lookups(), today=date(2026, 11, 7))
    assert stats["in_window"] == 0 and stats["staged"] == 0
    assert A.describe(stats).startswith("no new substitute trustee appointments (0 in the window")


def test_the_first_days_of_a_month_are_judged_on_the_month_before_too(tmp_path):
    rec = Recorder([])
    with pytest.raises(A.AppointmentsUnusable) as exc:
        run(tmp_path, rec, Lookups(), today=date(2026, 11, 3), months=None)
    assert [a[1] for a in rec.asked] == ["10/01/2026", "11/01/2026"], "two months are pulled early in the month"
    assert "34 days" in str(exc.value)


def test_the_quiet_window_rule_is_a_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOX_APPT_QUIET_DAYS", "0")
    assert run(tmp_path, Recorder([]), Lookups())["in_window"] == 0
    monkeypatch.setenv("KNOX_APPT_QUIET_DAYS", "20")
    assert run(tmp_path, Recorder([]), Lookups())["in_window"] == 0, "thirteen days is under twenty"
    monkeypatch.setenv("KNOX_APPT_QUIET_DAYS", "not a number")
    assert A.quiet_days() == 10


# ── the parcel id: only the exact parcel counts ───────────────────────


def test_a_parcel_id_counts_only_when_the_roll_answers_with_that_parcel():
    """The county's search is ranked by relevance and can put another parcel first."""
    rows = [{"account_number": "081JA-019", "parcel_address": "414 SAMPLE HOLLOW RD", "owner": "THORNBURY ESME"},
            {"account_number": "081JA-018", "parcel_address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE"}]
    got = A.parcel_by_id("081ja018", search=lambda pin: rows)
    assert got == {"address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE", "account": "081JA-018"}


def test_a_parcel_id_the_roll_does_not_hold_is_no_answer_at_all():
    near = [{"account_number": "081JA-019", "parcel_address": "414 SAMPLE HOLLOW RD", "owner": "THORNBURY ESME"}]
    assert A.parcel_by_id("081JA018", search=lambda pin: near) is None, "the neighbour's parcel is not this one"
    assert A.parcel_by_id("081JA018", search=lambda pin: []) is None
    assert A.parcel_by_id("", search=lambda pin: near) is None
    no_street = [{"account_number": "081JA-018", "parcel_address": "", "owner": "QUILLFEATHER MARLOWE"}]
    assert A.parcel_by_id("081JA018", search=lambda pin: no_street) is None


def test_an_id_that_does_not_resolve_falls_back_to_the_borrowers_name(tmp_path):
    rec = Recorder(appointment(APPT_1, DEED_1),
                   {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"], legal="PrpId:081JA018")})
    look = Lookups(parcels={}, by_name={"QUILLFEATHER MARLOWE": [
        {"account": "081JA-018", "owner": "QUILLFEATHER MARLOWE", "address": "412 SAMPLE HOLLOW RD"}]})
    stats = run(tmp_path, rec, look)
    assert look.parcel_asked == ["081JA018"] and stats["staged"] == 1
    assert "name on the tax roll" in ftm_staging.pending_rows(str(tmp_path), A.FEED)[0]["notes"]


def test_a_parcel_lookup_that_cannot_be_made_leaves_the_filing_open(tmp_path):
    """A failed request must never read as "no such parcel" or as "the house changed hands"."""
    def down(pin):
        raise OSError("connection reset")

    deed = A._parties(deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"], legal="PrpId:081JA018"))[DEED_1]
    found = A.resolve_parcel(deed, {"prp_ids": []}, lambda pin: A.parcel_by_id(pin, search=down), Lookups().name)
    assert found["outcome"] == "lookup failed" and "OSError" in found["error"]


def test_the_labels_are_settings(monkeypatch):
    monkeypatch.setenv("KNOX_ROD_APPT_LABEL", "subst")
    monkeypatch.setenv("KNOX_ROD_DOT_LABEL", "dot")
    assert A.appt_label() == "SUBST" and A.dot_label() == "DOT"


def test_the_report_line_counts_each_outcome(tmp_path):
    rows = appointment(APPT_1, DEED_1) + appointment(APPT_2, "", "11/05/2026")
    rec = Recorder(rows, {"03/15/2019": deed_of_trust(DEED_1, ["QUILLFEATHER MARLOWE"], legal="PrpId:081JA018")})
    look = Lookups(parcels={"081JA018": {"address": "412 SAMPLE HOLLOW RD", "owner": "QUILLFEATHER MARLOWE"}})
    said = A.describe(run(tmp_path, rec, look))
    assert said.startswith("2 new substitute trustee appointment(s)")
    assert "1 matched to a house" in said and "1 names no deed of trust the index can follow" in said


# ── the recorder module's additions ───────────────────────────────────


def test_a_doc_type_is_resolved_by_label_off_the_live_tree(monkeypatch):
    monkeypatch.setattr(knox_rod, "DOC_TREE", {})
    assert knox_rod.doc_code("LEN") == 5, "the lien types keep the numbers they always had"
    assert knox_rod.doc_code("ZZTOP") is None
    assert knox_rod.remember_tree([["77", "zztop"], ["x", "BAD"], ["78", ""]]) == 1
    assert knox_rod.doc_code("zztop") == 77


def test_an_unknown_doc_type_raises_instead_of_pulling_something_else(monkeypatch):
    monkeypatch.setattr(knox_rod, "DOC_TREE", {})
    with pytest.raises(ValueError) as exc:
        knox_rod.fetch_type({}, {}, "NOSUCH", "11/01/2026", "11/30/2026")
    assert "unknown doc type" in str(exc.value)


def test_a_second_stage_reuses_the_open_recorder_session(monkeypatch):
    monkeypatch.setattr(knox_rod, "SESSION", {})
    logins = []

    def login():
        logins.append(1)
        return {"SessionId": "s%d" % len(logins)}, {"h": "1"}

    clock = [1000.0]
    first = knox_rod.capture_cached(capture=login, clock=lambda: clock[0])
    clock[0] += 300
    second = knox_rod.capture_cached(capture=login, clock=lambda: clock[0])
    assert first == second and len(logins) == 1, "five minutes later the session is reused"
    clock[0] += knox_rod.SESSION_MAX_AGE + 1
    knox_rod.capture_cached(capture=login, clock=lambda: clock[0])
    assert len(logins) == 2, "a stale session is replaced by a fresh login"


def test_the_module_holds_no_long_dash():
    here = os.path.join(os.path.dirname(__file__), "..")
    for name in ("src/knox_appointments.py", "tests/test_knox_appointments.py"):
        text = open(os.path.join(here, name), encoding="utf-8").read()
        assert chr(0x2014) not in text and chr(0x2013) not in text and chr(8) not in text
