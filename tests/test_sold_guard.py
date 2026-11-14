"""Sold guard: a record is Sold only when the house sold AFTER it became a lead.
Zero network, invented names, streets and dates."""
import argparse
import copy
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import sold_guard as g  # noqa: E402

LISTS = g.OUR_LISTS


def created(day, tags=(), last_sold=None):
    return {"event_type": "property.created", "timestamp": day + "T04:10:00Z", "source": "bulk",
            "payload": {"property": {"sqft": 1200, "last_sold": last_sold}, "tags": list(tags)}}


def joined(day, *lists, system=False):
    key = "system_lists" if system else "lists"
    return {"event_type": "property.lists.added", "timestamp": day + "T15:24:00Z", "source": "internal-api",
            "payload": {key: list(lists)}}


def status(day, old, new, source="sequence"):
    return {"event_type": "property.status.updated", "timestamp": day + "T04:11:00Z", "source": source,
            "payload": {"property": {"status": [old, new]}}}


def rec(last_sold=None, st="sold", price="185000.00", tags=("recently sold",)):
    return {"last_sold": last_sold, "status": st, "last_sale_price": price, "tags": [{"title": t} for t in tags]}


def test_a_lead_whose_owner_bought_before_it_arrived_is_restored_to_its_old_status():
    ev = [created("2026-10-22", ["Tier 2", "Priority 2"]), status("2026-10-23", None, "sold")]
    d = g.decide(rec("2025-03-14"), ev, LISTS)
    assert (d["verdict"], d["action"], d["status_to"]) == ("own purchase", "restore", None)
    # whatever the sequence overwrote comes back, not a blank
    ev = [created("2026-08-01"), status("2026-08-05", None, "prospecting", "web"), status("2026-08-09", "prospecting", "sold")]
    assert g.decide(rec("2024-01-02"), ev, LISTS)["status_to"] == "prospecting"


def test_a_real_sale_after_the_lead_stays_sold():
    ev = [created("2026-07-22"), status("2026-11-10", None, "sold")]
    d = g.decide(rec("2026-10-16"), ev, LISTS)
    assert (d["verdict"], d["action"]) == ("sold since", "leave")
    # the sequence missed it: tagged, really sold, status still blank
    d = g.decide(rec("2026-10-16", st=""), [created("2026-07-22")], LISTS)
    assert (d["action"], d["status_to"]) == ("set sold", "sold")


def test_a_record_born_in_the_sold_pull_becomes_a_lead_only_when_it_joins_one_of_our_lists():
    born = created("2026-06-25", ["recently sold"])
    flip = status("2026-07-22", None, "sold", "bulk")
    # nobody ever pulled it: leave it alone
    d = g.decide(rec("2024-05-01"), [born, flip, joined("2026-10-16", "Negative Equity", system=True)], LISTS)
    assert (d["verdict"], d["action"]) == ("not a lead", "leave")
    # a foreclosure notice lands on the same address months later: the 2024 sale is the borrower's purchase
    ev = [born, flip, joined("2026-11-11", "Foreclosure")]
    d = g.decide(rec("2024-05-01"), ev, LISTS)
    assert (d["lead_day"], d["verdict"], d["action"]) == ("2026-11-11", "own purchase", "restore")
    # it joined our list and THEN sold: a real sale
    assert g.decide(rec("2026-12-02"), ev, LISTS)["verdict"] == "sold since"
    # DataSift's own distress lists count only when asked for
    ev = [born, flip, joined("2026-09-01", "Tax Delinquent")]
    assert g.decide(rec("2024-05-01"), ev, LISTS)["action"] == "leave"
    assert g.decide(rec("2024-05-01"), ev, LISTS + g.DISTRESS_LISTS)["action"] == "restore"


def test_a_person_marking_it_sold_is_never_undone_and_a_missing_date_decides_nothing():
    ev = [created("2026-08-01"), status("2026-09-01", "prospecting", "sold", "web")]
    d = g.decide(rec("2024-01-02"), ev, LISTS)
    assert (d["action"], d["verdict"]) == ("leave", "marked sold by a person")
    assert g.decide(rec(None), [created("2026-08-01"), status("2026-08-02", None, "sold")], LISTS)["verdict"] == "no sale date"
    # tagged but never flipped: only the tag comes off, the status is not touched
    d = g.decide(rec("2024-01-02", st="prospecting"), [created("2026-08-01")], LISTS)
    assert (d["action"], d["status_to"]) == ("untag", "prospecting")


def test_a_late_upload_never_revives_a_house_that_sold_after_its_own_notice():
    # the notice ran in July, the house sold at its auction in August, we uploaded it in November
    ev = [created("2026-11-11"), status("2026-11-12", None, "sold")]
    late = rec("2026-08-05", tags=("recently sold", "2026-07", "foreclosure", "Courthouse Data"))
    d = g.decide(late, ev, LISTS)
    assert (d["floor"], d["verdict"], d["action"]) == ("2026-07-01", "sold between the filing and our pull", "leave")
    # the same upload, but the borrower bought in 2022: his own purchase
    assert g.decide(rec("2022-09-02", tags=("recently sold", "2026-07", "foreclosure")), ev, LISTS)["action"] == "restore"
    # probate: an estate can sell months before the notice to creditors runs
    pr = lambda sold: g.decide(rec(sold, tags=("recently sold", "2026-10", "probate")), ev, LISTS)  # noqa: E731
    assert pr("2026-08-05")["action"] == "leave" and pr("2025-11-01")["action"] == "leave"
    assert pr("2025-09-15")["action"] == "restore"
    # a lien: only a sale older than the filing quarter is the debtor's own purchase
    ln = lambda sold: g.decide(rec(sold, tags=("recently sold", "filed_2025-Q4", "filed_2026-Q1")), ev, LISTS)  # noqa: E731
    assert ln("2026-02-01")["action"] == "leave" and ln("2025-09-30")["action"] == "restore"
    # an obituary on the record: only a sale before it is the owner's own purchase
    ob = dict(rec("2026-05-01"), last_obituary_date="2026-03-05")
    assert g.decide(ob, ev, LISTS)["action"] == "leave"
    assert g.decide(dict(ob, last_sold="2019-06-01"), ev, LISTS)["action"] == "restore"


def test_a_sale_learned_late_is_not_mistaken_for_the_owners_purchase():
    flip = status("2026-10-20", None, "sold")
    # pulled 10-19; a sale dated 10-15 reached the record later: the owner we hold is the seller
    d = g.decide(rec("2026-10-15"), [created("2026-10-19", last_sold="2014-03-02"), flip], LISTS)
    assert (d["verdict"], d["action"]) == ("sold just before we pulled it", "leave")
    # the same date, but the sale was already on the record at birth: a buyer we pulled on purpose
    d = g.decide(rec("2026-10-15"), [created("2026-10-19", last_sold="2026-10-15"), flip], LISTS)
    assert (d["verdict"], d["action"]) == ("own purchase", "restore")
    # an old sale filled in after birth is still the owner's own purchase
    d = g.decide(rec("2021-06-01"), [created("2026-10-19"), flip], LISTS)
    assert d["action"] == "restore"


def test_marking_needs_a_sale_after_the_lead_with_a_real_price():
    ev = [created("2026-07-22")]
    assert g.should_mark(rec("2026-10-16", st=""), ev, LISTS) == ""
    assert g.should_mark(rec("2026-10-16", st="", price="0.00"), ev, LISTS) == "no real price (family transfer)"
    assert g.should_mark(rec("2026-10-16", st="", price=None), ev, LISTS) == "no real price (family transfer)"
    assert g.should_mark(rec("2026-03-01", st=""), ev, LISTS) == "own purchase"
    # pulled 10-19 carrying a 2014 sale; a 10-15 sale reached the record later: the owner we hold sold it
    assert g.should_mark(rec("2026-10-15", st=""), [created("2026-10-19", last_sold="2014-03-02")], LISTS) == ""
    # nothing on the record at birth: cannot tell, so it is not marked
    assert g.should_mark(rec("2026-10-15", st=""), [created("2026-10-19")], LISTS) == "sold just before we pulled it"
    # the sale it was born with is the owner's own purchase, however recent
    assert g.should_mark(rec("2026-10-15", st=""), [created("2026-10-19", last_sold="2026-10-15")], LISTS) == "own purchase"


class FakeCrm:
    def __init__(self):
        self.calls = []

    def untag(self, uuid):
        self.calls.append(("untag", uuid))

    def tag(self, uuid):
        self.calls.append(("tag", uuid))

    def set_status(self, uuid, status):
        self.calls.append(("status", uuid, status))


def test_a_dry_run_writes_nothing_and_a_live_one_logs_the_prior_state_first(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    d = g.decide(rec("2025-03-14"), [created("2026-10-22"), status("2026-10-23", None, "sold")], LISTS)
    crm = FakeCrm()
    assert g.apply(crm, "u1", "12 Test Ln", d, commit=False) is False and crm.calls == [] and g.read_log() == []
    assert g.apply(crm, "u1", "12 Test Ln", d, commit=True) is True
    assert crm.calls == [("status", "u1", None), ("untag", "u1")]      # status first: a refusal changes nothing
    e = g.read_log()[0]
    assert (e["status_from"], e["status_to"], e["tag_removed"]) == ("sold", None, True)


def test_a_refused_write_is_logged_as_an_error_and_leaves_the_tag_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)

    class Refusing(FakeCrm):
        def set_status(self, uuid, status):
            raise RuntimeError("400 status may not be null")

    d = g.decide(rec("2025-03-14"), [created("2026-10-22"), status("2026-10-23", None, "sold")], LISTS)
    crm = Refusing()
    try:
        g.apply(crm, "u1", "12 Test Ln", d, commit=True)
        raise AssertionError("the refusal was swallowed")
    except RuntimeError:
        pass
    assert crm.calls == [] and g.read_log()[0]["error"].startswith("400")


def test_a_later_courthouse_upload_re_dates_the_lead_and_a_pull_list_does_not():
    # pulled in July, the owner sold in September (a real sale), then a notice names the NEW owner
    ev = [created("2026-07-01", last_sold="2010-01-01"), status("2026-10-01", None, "sold"),
          joined("2026-12-10", "Foreclosure")]
    d = g.decide(rec("2026-07-20", tags=("recently sold", "2026-12", "foreclosure")), ev, LISTS)
    assert (d["lead_day"], d["action"]) == ("2026-12-10", "restore")
    # a pull list leaves the owner on the record alone, so it cannot re-date an older record
    ev = [created("2026-07-01", last_sold="2010-01-01"), status("2026-10-01", None, "sold"),
          joined("2026-12-10", "D4D Top 3000")]
    d = g.decide(rec("2026-07-20"), ev, LISTS)
    assert (d["lead_day"], d["verdict"]) == ("2026-07-01", "sold since")


def test_sale_weeks_are_fixed_calendar_weeks_and_a_row_signature_moves_with_the_record():
    from datetime import date
    w = g.sale_weeks(date(2026, 11, 12))
    assert w[0] == ("2026-11-09", "2026-11-15") and w[1] == ("2026-11-02", "2026-11-08")
    assert g.sale_weeks(date(2026, 11, 13))[:2] == w[:2]            # the keys do not shift from day to day
    assert len(w) in (57, 58)
    a = {"list_count": 3, "tag_count": 5, "status": "sold"}
    assert g.row_sig(a) == g.row_sig(dict(a)) != g.row_sig(dict(a, list_count=4))


# ------------------------------------------------------------- the deed --

OWNER = {"first_name": "Tobias R", "last_name": "Quillfeather"}


def lien(last_sold=None, st="", tags=("recently sold", "filed_2026-Q3"), owner=OWNER, **extra):
    """A lien record we uploaded: the debtor is the owner of record, the filing quarter is the floor."""
    r = rec(last_sold, st=st, tags=tags)
    r.update(owner=dict(owner), dataflik_id="9000001", **extra)
    return r


def deed(day, buyer, price=185000, seller=""):
    return {"date": day, "price": price, "buyer": buyer, "seller": seller}


def replaced(day, source="bulk", event="property.owner.replaced", payload=None):
    return {"event_type": event, "timestamp": day + "T04:10:00Z", "source": source, "payload": payload or {}}


UPLOADED = [created("2026-10-20"), joined("2026-10-20", "Liens")]


def test_a_deed_name_matches_the_owner_in_any_form():
    own = g.owner_names({"owner": OWNER})
    for name in ("QUILLFEATHER TOBIAS R", "Tobias R. Quillfeather", "QUILLFEATHER TOBIAS & MAREN",
                 "TOBIAS AND MAREN QUILLFEATHER", "TOBIAS R QUILLFEATHER REVOCABLE LIVING TRUST",
                 "ESTATE OF TOBIAS QUILLFEATHER", "QUILLFEATHER T", "Quillfeather, Tobias et al"):
        assert g.name_match(own, name) == "owner", name
    # the wife alone, a son, or the family trust share the surname and are not the owner we hold
    for name in ("QUILLFEATHER MAREN J", "QUILLFEATHER FAMILY TRUST", "MAREN QUILLFEATHER",
                 "QUILLFEATHER MAREN & BRISTOW TOBIAS"):
        assert g.name_match(own, name) == "surname", name
    assert g.name_match(own, "BRISTOW HOLDINGS LLC") == "stranger"
    assert g.name_match(own, "") == "nobody" and g.name_match([], "QUILLFEATHER TOBIAS") == "no owner"
    # a surname spelled another way is never a stranger: that would mark a live lead Sold on a typo
    assert g.name_match(own, "QUILFEATHER TOBIAS") == "surname"
    obr = g.owner_names({"owner": {"first_name": "Ansel", "last_name": "O'Brannick"}})
    assert g.name_match(obr, "OBRANNICK ANSEL") == "surname" and g.name_match(obr, "O BRANNICK ANSEL") == "owner"
    assert g._one_edit("QUILLFEATHER", "QUILFEATHER") and not g._one_edit("QUILLFEATHER", "QUILFETHER")
    # an initial on the record still matches the full name on the deed
    ini = g.owner_names({"owner": {"first_name": "T", "last_name": "Quillfeather"}})
    assert g.name_match(ini, "QUILLFEATHER TOBIAS") == "owner"
    # a secondary owner on the record counts: a deed to either spouse is the household's own
    two = g.owner_names({"owner": OWNER, "secondary_owners": [{"full_name": "Maren Quillfeather"}]})
    assert g.name_match(two, "QUILLFEATHER MAREN J") == "owner"
    # a company must be there whole; a shared distinctive word is not enough, a generic one is nothing
    co = g.owner_names({"owner": {"company": "Lanternfield Homes LLC"}})
    assert g.name_match(co, "LANTERNFIELD HOMES L.L.C.") == "owner"
    assert g.name_match(co, "LANTERNFIELD BUILDERS INC") == "partial"
    assert g.name_match(co, "ORCHARD HOMES LLC") == "stranger"


def test_the_lien_bought_inside_its_filing_quarter_loses_the_tag_on_the_deed():
    # the owner's second record: bought 08-11 inside the Q3 filing quarter, tagged, no status
    r = lien("2026-08-11")
    old = g.decide(r, UPLOADED, LISTS)
    assert (old["verdict"], old["action"]) == ("sold between the filing and our pull", "leave")
    d = g.decide(r, UPLOADED, LISTS, deed=deed("2026-08-11", "QUILLFEATHER TOBIAS R"))
    assert (d["action"], d["verdict"]) == ("untag", "own purchase: the newest deed names the owner")
    # the first record: bought before the quarter; right before, still right
    d = g.decide(lien("2026-04-14"), UPLOADED, LISTS, deed=deed("2026-04-14", "QUILLFEATHER TOBIAS"))
    assert d["action"] == "untag"
    # the third record: bought the year before and never tagged, so there is nothing to fix
    d = g.decide(lien("2025-08-28", tags=("filed_2026-Q3",)), UPLOADED, LISTS,
                 deed=deed("2025-08-28", "QUILLFEATHER TOBIAS"))
    assert (d["action"], d["tagged"]) == ("leave", False) and d["verdict"].startswith("own purchase")


def test_a_stale_last_sold_gives_way_to_the_deed():
    # the fourth record: DataSift still shows a 2021 sale, the deed a 2026 purchase by our owner,
    # and the duplicate sequence set Sold: the status comes back for the right reason now
    ev = UPLOADED + [status("2026-11-12", None, "sold")]
    r = lien("2021-05-02", st="sold", tags=("filed_2026-Q3",))
    d = g.decide(r, ev, LISTS, deed=deed("2026-08-17", "QUILLFEATHER TOBIAS R"))
    assert (d["action"], d["status_to"], d["sale_day"], d["tagged"]) == ("restore", None, "2026-08-17", False)
    # a stale last_sold also hid a real sale: the dates call it the owner's purchase, the deed a sale
    ev = [created("2026-07-01"), status("2026-11-12", None, "sold")]
    plain = lien("2019-03-01", st="sold", tags=("recently sold",))
    assert g.decide(plain, ev, LISTS)["action"] == "restore"
    d = g.decide(plain, ev, LISTS, deed=deed("2026-09-12", "BRISTOW HOLDINGS LLC", 240000))
    assert (d["action"], d["verdict"]) == ("leave", "sold since: the newest deed names someone else")
    # the deed day stands in for an older last_sold; a last_sold a little newer is the same sale;
    # one much newer is a sale the deed has not reached, and then the dates decide
    assert g.deed_sale_day(deed("2026-08-17", "X"), "2021-05-02") == "2026-08-17"
    assert g.deed_sale_day(deed("2026-08-17", "X"), "2026-09-10") == "2026-09-10"
    assert g.deed_sale_day(deed("2026-08-17", "X"), "2026-12-30") == ""
    assert g.deed_sale_day({}, "2026-08-17") == "" and g.deed_sale_day(None, "") == ""
    d = g.decide(lien("2026-08-11"), UPLOADED, LISTS, deed=deed("2025-01-03", "QUILLFEATHER TOBIAS"))
    assert (d["verdict"], d["deed"]) == ("sold between the filing and our pull", "older than the last sale")


def test_a_relative_buying_the_family_house_is_left_for_a_person():
    # the fifth record: probate, bought by a family member with the same surname, Sold by a sequence
    ev = [created("2026-11-11"), joined("2026-11-11", "Probate"), status("2026-11-12", None, "sold")]
    r = rec("2026-07-29", st="sold", tags=("2026-10", "probate"))
    r["owner"] = {"first_name": "Corwin", "last_name": "Pellwright"}
    d = g.decide(r, ev, LISTS, deed=deed("2026-07-29", "PELLWRIGHT JUNA M", 0))
    assert (d["action"], d["person"]) == ("leave", "the buyer on the newest deed shares only the surname")
    # a deed with no buyer, or a record with no owner, cannot be judged either
    assert g.decide(r, ev, LISTS, deed=deed("2026-07-29", ""))["person"] == "the newest deed names no buyer"
    bare = dict(r, owner={})
    assert g.decide(bare, ev, LISTS, deed=deed("2026-07-29", "PELLWRIGHT JUNA"))["person"].startswith("the record names no owner")
    # the usual probate record: the decedent bought decades ago and the personal representative we
    # hold is not on that deed; that is the family's own purchase, not a case for a person
    d = g.decide(dict(r, last_sold="1994-05-02"), ev, LISTS, deed=deed("1994-05-02", "PELLWRIGHT ORVILLE & EDNA", 64000))
    assert (d["action"], d.get("person"), d["status_to"]) == ("restore", None, None)


def test_a_stranger_buying_after_the_lead_is_a_real_sale():
    ev = [created("2026-07-22")]
    buy = deed("2026-10-16", "BRISTOW HOLDINGS LLC", 210000)
    tagged = rec("2026-10-16", st="", tags=("recently sold",))
    tagged["owner"] = dict(OWNER)
    assert g.decide(tagged, ev, LISTS, deed=buy)["action"] == "set sold"
    untagged = dict(tagged, tags=[])
    d = g.decide(untagged, ev, LISTS, deed=buy)
    assert (d["action"], d["status_to"]) == ("mark", "sold")
    assert g.decide(dict(tagged, status="sold"), ev + [status("2026-10-20", None, "sold")], LISTS, deed=buy)["action"] == "leave"
    # noticed in July, sold at its own auction in August, uploaded in November: a real sale now
    late = rec(None, st="", tags=("recently sold", "2026-07", "foreclosure"))
    late["owner"] = dict(OWNER)
    up = [created("2026-11-11")]
    d = g.decide(late, up, LISTS, deed=deed("2026-08-05", "ORCHARD RENTALS LLC", 151000))
    assert (d["action"], d["verdict"]) == ("set sold", "sold after the filing: the newest deed names someone else")
    # a deed years older than the filing is the current owner's purchase whoever it names
    d = g.decide(late, up, LISTS, deed=deed("2018-03-05", "ORCHARD RENTALS LLC"))
    assert (d["action"], d["verdict"]) == ("untag", "own purchase: the newest deed is older than the filing")
    # a sale learned late whose seller is our owner is a real sale; a stranger selling to a stranger
    # weeks before we pulled it means the owner we hold is not on the deed at all: a person looks
    pulled = dict(untagged, last_sold=None)
    d = g.decide(pulled, [created("2026-10-19")], LISTS,
                 deed=deed("2026-10-15", "ORCHARD RENTALS LLC", 199000, seller="QUILLFEATHER TOBIAS"))
    assert (d["action"], d["verdict"]) == ("mark", "sold just before we pulled it: the newest deed names the owner as the seller")
    d = g.decide(pulled, [created("2026-10-19")], LISTS,
                 deed=deed("2026-10-15", "ORCHARD RENTALS LLC", 199000, seller="FENWICK DALE"))
    assert (d["action"], d["person"]) == ("leave", "the newest deed names someone else, dated just before the lead")
    # changing hands for nothing is not a sale anyone can act on unseen; DataSift's own price counts
    # only when it is the same sale
    free = dict(untagged, last_sale_price=None)
    d = g.decide(free, ev, LISTS, deed=deed("2026-10-16", "BRISTOW HOLDINGS LLC", 0))
    assert (d["action"], d["person"]) == ("leave", "the house changed hands with no real price")
    assert g.decide(untagged, ev, LISTS, deed=deed("2026-10-16", "BRISTOW HOLDINGS LLC", 0))["action"] == "mark"


def test_an_owner_a_machine_copied_in_after_the_sale_proves_nothing():
    buy = deed("2026-09-20", "QUILLFEATHER TOBIAS")
    r = rec("2026-09-20", st="sold")
    r["owner"] = dict(OWNER)
    base = [created("2026-07-01"), status("2026-10-03", None, "sold")]
    d = g.decide(r, base + [replaced("2026-10-03")], LISTS, deed=buy)
    assert (d["action"], d["person"]) == ("leave", "the owner was replaced by a bulk add after the sale")
    # our own uploader choosing the owner is not the trap, nor is a copy made before the sale
    assert g.decide(r, base + [replaced("2026-10-03", source="internal-api")], LISTS, deed=buy)["action"] == "restore"
    assert g.decide(r, base + [replaced("2026-08-01")], LISTS, deed=buy)["action"] == "restore"
    # an owner.updated from a machine counts only when it changed a name
    renamed = replaced("2026-10-03", event="owner.updated",
                       payload={"owner": {"last_name": ["Bristow", "Quillfeather"]}})
    assert g.decide(r, base + [renamed], LISTS, deed=buy)["person"].startswith("the owner was replaced")
    moved = replaced("2026-10-03", event="owner.updated", payload={"owner": {"mailing_city": ["A", "B"]}})
    assert g.decide(r, base + [moved], LISTS, deed=buy)["action"] == "restore"
    # a copy made after the sale but BEFORE the record became our lead overwrote nobody we worked:
    # the uploader set the owner on the lead day, so the deed still speaks
    lien_ev = UPLOADED + [status("2026-11-12", None, "sold")]
    r2 = lien("2026-08-11", st="sold")
    early = g.decide(r2, lien_ev + [replaced("2026-09-01")], LISTS, deed=deed("2026-08-11", "QUILLFEATHER TOBIAS"))
    assert (early["action"], early.get("person")) == ("restore", None)
    late = g.decide(r2, lien_ev + [replaced("2026-10-25")], LISTS, deed=deed("2026-08-11", "QUILLFEATHER TOBIAS"))
    assert (late["action"], late["owner_copied"]) == ("leave", "2026-10-25")


def test_the_guard_may_undo_a_sold_it_set_itself_but_never_a_persons():
    log = [{"uuid": "u7", "action": "set sold", "status_from": "", "status_to": "sold"},
           {"uuid": "u8", "action": "mark", "status_from": "prospecting", "status_to": "sold"},
           {"uuid": "u8", "action": "restore", "status_from": "sold", "status_to": "prospecting"},
           {"uuid": "u9", "action": "mark", "status_from": "sold", "status_to": "sold"},
           {"uuid": "u10", "action": "mark", "status_from": "", "status_to": "sold", "error": "400"},
           {"action": "preset auto-add off", "preset": 4076}]
    own = g.guard_sold_flips(log)
    assert own == {"u7": None}                  # u8 came off Sold again, u9 was Sold already, u10 failed
    # the guard set this lien Sold on the dates alone; its deed names the owner: the guard undoes it
    ours = dict(lien("2026-08-11", st="sold"), uuid="u7")
    ev = UPLOADED + [status("2026-11-12", None, "sold", "internal-api")]
    buy = deed("2026-08-11", "QUILLFEATHER TOBIAS")
    assert g.needs_deed(ours, ev, LISTS, own) is True and g.needs_deed(ours, ev, LISTS) is False
    d = g.decide(ours, ev, LISTS, deed=buy, own_flips=own)
    assert (d["action"], d["status_to"], d["undoes"]) == ("restore", None, "a Sold the guard set itself")
    # the same flip with no line in the guard's log, or a person's flip, is a person's call
    assert g.decide(ours, ev, LISTS, deed=buy)["verdict"] == "marked sold by a person"
    web = UPLOADED + [status("2026-11-12", None, "sold", "web")]
    assert g.decide(ours, web, LISTS, deed=buy, own_flips=own)["verdict"] == "marked sold by a person"


def test_a_deed_is_read_only_where_it_can_change_the_answer():
    r = rec("2025-01-02", st="sold")
    born = created("2026-06-25", ["recently sold"])
    assert g.needs_deed(r, [born], LISTS) is False                                     # never a lead
    assert g.needs_deed(r, [created("2026-08-01"), status("2026-09-01", "prospecting", "sold", "web")], LISTS) is False
    assert g.needs_deed(r, [created("2026-08-01"), status("2026-09-01", None, "sold")], LISTS) is True
    assert g.needs_deed(rec("2025-01-02", st=""), [created("2026-08-01")], LISTS) is True


def test_the_newest_deed_is_the_latest_dated_row():
    detail = {"sale_history": [{"sale_date": "2019-01-02", "buyer_name": "OLD OWNER", "sale_price": 90000},
                               {"sale_date": "2026-08-19T00:00:00", "buyer_name": " NEW OWNER ", "sale_price": 1}]}
    assert g.newest_deed(detail) == {"date": "2026-08-19", "price": 1, "buyer": "NEW OWNER", "seller": ""}
    assert g.newest_deed({"sale_history": []}) == {} and g.newest_deed(None) == {}


class Map:
    """A fake SiftMap: recent-sale search rows, and a deed per dataflik id."""

    def __init__(self, rows, deeds, floor_bites=True):
        self.rows, self.deeds, self.floor_bites = rows, deeds, floor_bites
        self.details, self.searches = [], []

    def search(self, body):
        self.searches.append(body)
        bites = "extra_last_sale_date_min" in body["filters"] and self.floor_bites
        if body["size"] == 1:
            return {"total_results": len(self.rows) if bites else len(self.rows) + 900}
        start = body["result_index"]
        return {"data": self.rows[start:start + body["size"]], "total_results": len(self.rows)}

    def get_detail(self, pid):
        self.details.append(pid)
        return {"sale_history": [self.deeds[pid]]} if pid in self.deeds else {"sale_history": []}


class PassCrm(FakeCrm):
    """A fake account for whole passes: records, their logs, and every write applied in memory."""

    def __init__(self, records, logs, tagged_rows=()):
        super().__init__()
        self.records, self.logs, self.rows, self.reads = records, logs, list(tagged_rows), 0

    def titles(self, path):
        return {"recently sold": "tag-rs"} if "/tag/" in path else {"Liens": "list-liens"}

    def page(self, must):
        return [dict(r) for r in self.rows] if "created" in must else []

    def record(self, uuid):
        self.reads += 1
        return copy.deepcopy(self.records[uuid])

    def events(self, uuid):
        return list(self.logs.get(uuid, []))

    def untag(self, uuid):
        super().untag(uuid)
        self.records[uuid]["tags"] = [t for t in self.records[uuid]["tags"] if t["title"] != "recently sold"]

    def tag(self, uuid):
        super().tag(uuid)
        self.records[uuid]["tags"].append({"title": "recently sold"})

    def set_status(self, uuid, status):
        super().set_status(uuid, status)
        self.records[uuid]["status"] = status or ""


TODAY = date(2026, 11, 14)          # a Saturday; its week starts 2026-11-09


def house(n, tags=()):
    return {"uuid": "s%d" % n, "status": "", "last_sold": None, "last_sale_price": None,
            "tags": [{"title": t} for t in tags], "owner": dict(OWNER), "dataflik_id": "d%d" % n,
            "address": {"street": "%d Invented Way" % n}}


def search_args(crm, fmap, commit, cap, cache=None):
    a = argparse.Namespace(commit=commit, limit=0, include_distress=False, fresh=False, stats={}, crm=crm,
                           siftmap=fmap, today=TODAY)
    a.cache = cache or g._cache()
    a.deeds = g.Deeds(a.cache["deeds"], client=fmap, cap=cap, today=TODAY)
    return a


def recent_world(floor_bites=True):
    rows = [{"id": "d%d" % n, "saved_uuid": "s%d" % n, "address": "%d Invented Way, Testville, TN" % n} for n in range(1, 6)]
    rows.append({"id": "d99", "saved_uuid": None, "address": "99 Elsewhere Rd, Testville, TN"})
    deeds = {"d%d" % n: {"sale_date": "2026-10-20", "sale_price": 199000, "buyer_name": "BRISTOW HOLDINGS LLC"}
             for n in range(1, 6)}
    records = {"s%d" % n: house(n) for n in range(1, 6)}
    logs = {"s%d" % n: [created("2026-07-01")] for n in range(1, 6)}
    return rows, Map(rows, deeds, floor_bites), records, logs


def test_the_nightly_search_proves_its_floor_judges_each_house_once_a_week_and_stops_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    monkeypatch.setattr(g, "COUNTIES", (("00001", "Testcounty", "TN"),))
    monkeypatch.setattr(g, "SEARCH_PAGE", 2)
    rows, fmap, records, logs = recent_world()
    crm = PassCrm(records, logs)
    cache = g._cache()
    cache["detect"]["s5"] = {"week": "2026-11-09", "why": "own purchase"}     # judged earlier this week
    a = search_args(crm, fmap, commit=False, cap=2, cache=cache)
    assert g.detect(a) == 0
    st = a.stats
    assert fmap.details == ["d1", "d2"]                      # the cap held: two deed reads, no more
    assert (st["detect_rows"], st["detect_outside"], st["detect_judged"], st["detect_held"]) == (5, 1, 2, 2)
    assert st["detect_would"] == 2 and crm.calls == []       # a dry run writes nothing
    floor = [b["filters"].get("extra_last_sale_date_min") for b in fmap.searches if b["size"] == 1]
    assert floor == ["2026-09-30", None]                     # proven by a count that moved, 45 days back
    assert all(b["filters"]["in_my_account_mode"] == "in" and b["filters"]["type_single_family"]
               and b["filters"]["extra_is_last_sale_interfamily"] is False for b in fmap.searches)
    # live, on a fresh account: the two judged houses are tagged and set Sold, and remembered this week
    monkeypatch.setattr(g, "OUT", tmp_path / "live")
    rows, fmap, records, logs = recent_world()
    crm = PassCrm(records, logs)
    a = search_args(crm, fmap, commit=True, cap=2)
    assert g.detect(a) == 0
    assert crm.calls == [("status", "s1", "sold"), ("tag", "s1"), ("status", "s2", "sold"), ("tag", "s2")]
    assert a.cache["detect"]["s1"]["week"] == "2026-11-09" and "s3" not in a.cache["detect"]
    assert a.stats["detect_marked"] == 2 and a.cache["deeds"]["d1"]["deed"]["buyer"] == "BRISTOW HOLDINGS LLC"


def test_a_search_whose_date_floor_is_ignored_reads_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    monkeypatch.setattr(g, "COUNTIES", (("00001", "Testcounty", "TN"),))
    rows, fmap, records, logs = recent_world(floor_bites=False)
    crm = PassCrm(records, logs)
    a = search_args(crm, fmap, commit=True, cap=150)
    assert g.detect(a) == 0
    assert fmap.details == [] and crm.reads == 0 and crm.calls == []
    assert "did not change the count" in a.stats["detect_note"] and a.stats["detect_rows"] == 0


def test_the_repair_pass_keeps_reads_for_the_search_and_counts_what_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    records, logs, tagged = {}, {}, []
    for n in (1, 2, 3):
        r = house(n, tags=("recently sold", "filed_2026-Q3"))
        r["last_sold"] = "2026-08-11"
        records["s%d" % n], logs["s%d" % n] = r, list(UPLOADED)
        tagged.append({"uuid": "s%d" % n, "address": r["address"], "list_count": 2, "tag_count": 2, "status": ""})
    fmap = Map([], {"d%d" % n: {"sale_date": "2026-08-11", "sale_price": 99000, "buyer_name": "QUILLFEATHER TOBIAS"}
                    for n in (1, 2, 3)})
    crm = PassCrm(records, logs, tagged)
    a = search_args(crm, fmap, commit=False, cap=g.DETECT_RESERVE + 1)
    assert g.repair(a) == 0
    assert fmap.details == ["d1"]                            # the reserve is kept for the sale search
    assert (a.stats["repair_judged"], a.stats["repair_held"], a.stats["repair_would"]) == (1, 2, 1)
    plan = json.loads((tmp_path / "repair_plan.json").read_text(encoding="utf-8"))
    assert [(p["uuid"], p["action"]) for p in plan] == [("s1", "untag")]


def test_a_deed_read_is_reused_for_a_week_and_a_refused_key_stops_the_reads():
    fmap = Map([], {"d1": {"sale_date": "2026-08-11", "buyer_name": "QUILLFEATHER TOBIAS"}})
    store = {"d2": {"at": "2026-11-10", "deed": {"date": "2026-01-02", "buyer": "X"}},
             "d3": {"at": "2026-10-01", "deed": {"date": "2025-01-02", "buyer": "Y"}}}
    dd = g.Deeds(store, client=fmap, cap=3, today=TODAY)
    assert dd.get("") is None and dd.get(None) is None            # no SiftMap id, nothing to read
    assert dd.get("d2")["buyer"] == "X" and fmap.details == []    # four days old: reused
    assert dd.get("d3") == {} and fmap.details == ["d3"]          # six weeks old: read again
    assert store["d3"]["at"] == "2026-11-14" and dd.reads == 1

    class Refusing(Map):
        def get_detail(self, pid):
            raise RuntimeError("401 on GET /properties/detail/x/. The Api-Key was rejected.")

    dd = g.Deeds({}, client=Refusing([], {}), cap=10, today=TODAY)
    try:
        dd.get("d1")
        raise AssertionError("the refusal was swallowed")
    except RuntimeError:
        pass
    try:
        dd.get("d4")
        raise AssertionError("a refused key kept reading")
    except g.HeldBack as hb:
        assert "refused the key" in str(hb)


def test_verdicts_made_before_the_deed_are_judged_again(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    (tmp_path / "judged.json").write_text(json.dumps({"repair": {"u1": "3|8|"}, "mark": {"u2|2026-11-02": "own purchase"},
                                                     "reentry": {"u3": "2|4|sold"}}), encoding="utf-8")
    c = g._cache()
    assert (c["v"], c["repair"], c["mark"], c["reentry"]) == (2, {}, {}, {"u3": "2|4|sold"})
    c["repair"]["u1"] = "3|8|"
    g._save_cache(c)
    assert g._cache()["repair"] == {"u1": "3|8|"}             # a verdict made with the deed stays


def test_the_person_list_holds_a_record_until_a_later_judgment_decides_it(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    a = argparse.Namespace(stats={}, today=TODAY)
    g._person(a, "u5", "5 Invented Way", {"person": "the buyer on the newest deed shares only the surname",
                                         "status": "sold", "tagged": False})
    g._write_person(a)
    held = json.loads((tmp_path / "for_a_person.json").read_text(encoding="utf-8"))
    assert held["u5"]["first_seen"] == "2026-11-14" and a.stats["for_a_person"] == 1
    assert "shares only the surname" in (tmp_path / "for_a_person.csv").read_text(encoding="utf-8")
    g._person(a, "u5", "5 Invented Way", {"verdict": "own purchase", "action": "restore"})
    g._write_person(a)
    assert json.loads((tmp_path / "for_a_person.json").read_text(encoding="utf-8")) == {}
    assert (a.stats["for_a_person"], a.stats["for_a_person_new"]) == (0, 1)


def test_a_restore_without_the_tag_never_touches_tags_and_undo_does_not_add_one(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path)
    ev = UPLOADED + [status("2026-11-12", None, "sold")]
    d = g.decide(lien("2021-05-02", st="sold", tags=("filed_2026-Q3",)), ev, LISTS,
                 deed=deed("2026-08-17", "QUILLFEATHER TOBIAS"))
    crm = FakeCrm()
    assert g.apply(crm, "u4", "4 Invented Way", d, commit=True) is True
    assert crm.calls == [("status", "u4", None)]
    e = g.read_log()[0]
    assert (e["tag_removed"], e["tag_added"], e["deed_day"]) == (False, False, "2026-08-17")
    back = FakeCrm()
    monkeypatch.setattr(g, "Crm", lambda: back)
    g.undo(argparse.Namespace(commit=True))
    assert back.calls == [("status", "u4", "sold")]


def test_daily_runs_the_search_between_repair_and_mark_and_keeps_the_runner_keys(tmp_path, monkeypatch):
    import ftm_runner as r
    monkeypatch.setattr(g, "OUT", tmp_path)
    monkeypatch.setattr(g, "COUNTIES", (("00001", "Testcounty", "TN"),))
    rows, fmap, records, logs = recent_world()
    monkeypatch.setattr(g, "Crm", lambda: PassCrm(records, logs))
    monkeypatch.setattr(g, "_siftmap", lambda: fmap)
    st = g.daily(commit=False)
    for k in ("rc", "restored", "marked", "repair_judged", "mark_judged", "repair_errors", "mark_errors",
              "held_back", "deed_reads", "for_a_person"):
        assert k in st, k
    assert (st["rc"], st["detect_judged"], st["deed_reads"], st["mark_judged"]) == (0, 5, 5, 5)
    res = r._stage_sold(argparse.Namespace(commit=False))
    assert res.status == "ok" and "judged" in res.detail


def test_the_audit_stays_read_only_and_shows_tonights_slice(monkeypatch, capsys):
    monkeypatch.setattr(g, "COUNTIES", (("00001", "Testcounty", "TN"),))
    rows, fmap, records, logs = recent_world()

    class Counting(PassCrm):
        def search(self, must, limit=1, offset=0):
            return {"count": 7, "results": []}

    crm = Counting(records, logs)
    monkeypatch.setattr(g, "Crm", lambda: crm)
    monkeypatch.setattr(g, "_siftmap", lambda: fmap)
    assert g.audit(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "sale search, Testcounty: 6 house(s)" in out and "floor proven" in out
    assert crm.calls == [] and fmap.details == []            # counts only: no write, no deed read


def test_the_detect_command_is_wired(monkeypatch):
    seen = []
    monkeypatch.setattr(g, "detect", lambda a: seen.append((a.cmd, a.commit, a.limit)) or 0)
    assert g.main(["detect", "--limit", "3"]) == 0 and seen == [("detect", False, 3)]


def test_the_daily_runner_carries_the_sold_stage_last():
    import ftm_runner as r
    assert r.ALL_STAGES[-1] == "sold" and r.STAGE_FUNCS["sold"] is r._stage_sold


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/sold_guard.py", "tests/test_sold_guard.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
