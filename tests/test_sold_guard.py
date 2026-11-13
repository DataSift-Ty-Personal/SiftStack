"""Sold guard: a record is Sold only when the house sold AFTER it became a lead.
Zero network, invented streets and dates."""
import sys
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


def test_the_daily_runner_carries_the_sold_stage_last():
    import ftm_runner as r
    assert r.ALL_STAGES[-1] == "sold" and r.STAGE_FUNCS["sold"] is r._stage_sold


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/sold_guard.py", "tests/test_sold_guard.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
