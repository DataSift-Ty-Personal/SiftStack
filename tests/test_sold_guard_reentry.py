"""Sold guard: the January re-entry and the monthly audit (build plan 0.7).

Zero network. A fake CRM holds a handful of invented records and remembers every write, and the
change log lands in a temp folder. Streets, dates and prices are invented.

Run: python -m pytest tests/test_sold_guard_reentry.py
"""
import argparse
import csv
import importlib
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import sold_guard as g  # noqa: E402

CUTOFF = "2024-01-01"            # what a three year window reads on any day of 2027
JAN = date(2027, 1, 4)


def created(day, tags=(), last_sold=None):
    return {"event_type": "property.created", "timestamp": day + "T04:10:00Z", "source": "bulk",
            "payload": {"property": {"last_sold": last_sold}, "tags": list(tags)}}


def status(day, old, new, source="sequence"):
    return {"event_type": "property.status.updated", "timestamp": day + "T04:11:00Z", "source": source,
            "payload": {"property": {"status": [old, new]}}}


def joined(day, *lists):
    return {"event_type": "property.lists.added", "timestamp": day + "T15:24:00Z", "source": "internal-api",
            "payload": {"lists": list(lists)}}


def rec(last_sold="2023-05-01", st="sold", lists=(), owned_since=None, **kw):
    base = {"last_sold": last_sold, "status": st, "last_sale_price": "185000.00",
            "tags": [{"title": "recently sold"}], "lists": [{"title": x} for x in lists],
            "owned_since": owned_since, "address": {"street": "412 Sample Hollow Rd"}}
    base.update(kw)
    return base


BORN = [created("2026-06-25", ["recently sold"], "2023-05-01"), status("2026-07-22", None, "sold", "bulk")]


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "OUT", tmp_path / "sold_guard")
    for name in ("SOLD_REENTRY_MONTHS", "SOLD_REENTRY_STATUS"):
        monkeypatch.delenv(name, raising=False)


# ── the window ────────────────────────────────────────────────────────


def test_the_window_moves_once_a_year_on_january_first():
    assert g.reentry_cutoff(date(2027, 1, 1)) == "2024-01-01"
    assert g.reentry_cutoff(date(2027, 12, 31)) == "2024-01-01", "it does not creep through the year"
    assert g.reentry_cutoff(date(2026, 11, 13)) == "2023-01-01"


def test_the_pass_is_due_in_january_only_unless_widened(monkeypatch):
    assert g.reentry_due(date(2027, 1, 31)) and not g.reentry_due(date(2027, 2, 1))
    monkeypatch.setenv("SOLD_REENTRY_MONTHS", "2")
    assert g.reentry_due(date(2027, 2, 20)) and not g.reentry_due(date(2027, 3, 1))


def test_the_search_is_cut_into_whole_months_ending_at_the_cutoff():
    months = g.reentry_months(CUTOFF)
    assert months[0] == ("2023-12-01", "2023-12-31") and months[11] == ("2023-01-01", "2023-01-31")
    assert months[-1] == ("2022-01-01", "2022-01-31") and len(months) == 24
    assert all(a[:8] + "01" == a for a, _b in months)


# ── one record ────────────────────────────────────────────────────────


def test_a_house_sold_before_the_window_comes_back_as_the_new_owners_record():
    d = g.reentry_decide(rec("2023-05-01"), BORN, CUTOFF)
    assert (d["action"], d["verdict"], d["status_to"]) == ("reenter", "sold before the window", "")


def test_a_sale_inside_the_window_stays_sold():
    d = g.reentry_decide(rec("2024-01-01"), BORN, CUTOFF)
    assert (d["action"], d["verdict"]) == ("leave", "sold inside the window")
    assert g.reentry_decide(rec("2026-10-16"), BORN, CUTOFF)["action"] == "leave"


def test_a_sold_a_person_set_is_never_undone():
    ev = [created("2026-06-25", ["recently sold"]), status("2026-07-22", "prospecting", "sold", "web")]
    assert g.reentry_decide(rec(), ev, CUTOFF)["verdict"] == "marked sold by a person"


def test_a_record_whose_owner_is_still_the_seller_does_not_come_back():
    """Not born in the sold pull, so nothing replaced its owner. Re-entering it would call a
    stranger's house and ask for the person who sold it three years ago."""
    ev = [created("2022-09-01"), status("2023-06-10", "prospecting", "sold")]
    d = g.reentry_decide(rec("2023-05-01", owned_since="2015-03-02"), ev, CUTOFF)
    assert d["verdict"] == "the owner on the record is from before the sale"
    assert g.reentry_decide(rec("2023-05-01", owned_since=None), ev, CUTOFF)["action"] == "leave"


def test_a_record_whose_owner_dates_from_the_sale_does_come_back():
    ev = [created("2022-09-01"), status("2023-06-10", "prospecting", "sold")]
    assert g.reentry_decide(rec("2023-05-01", owned_since="2023-05-01"), ev, CUTOFF)["action"] == "reenter"
    assert g.reentry_decide(rec("2023-05-01", owned_since="2023-04-10"), ev, CUTOFF)["action"] == "reenter"


def test_a_record_on_a_courthouse_list_is_left_for_a_person():
    d = g.reentry_decide(rec(lists=["Foreclosure", "Absentee Owners"]), BORN, CUTOFF)
    assert d["verdict"] == "on a courthouse list from the previous owner"
    assert g.reentry_decide(rec(lists=["Absentee Owners"]), BORN, CUTOFF)["action"] == "reenter"


def test_what_is_not_sold_or_has_no_sale_date_is_left_alone():
    assert g.reentry_decide(rec(st="prospecting"), BORN, CUTOFF)["verdict"] == "not marked Sold"
    assert g.reentry_decide(rec(last_sold=None), BORN, CUTOFF)["verdict"] == "no sale date"
    assert g.reentry_decide(rec(), [created("2026-07-01")], CUTOFF)["verdict"] == "no record of who marked it sold"


def test_the_status_a_record_returns_to_is_a_setting(monkeypatch):
    monkeypatch.setenv("SOLD_REENTRY_STATUS", "new_owner")
    assert g.reentry_decide(rec(), BORN, CUTOFF)["status_to"] == "new_owner"


# ── the pass ──────────────────────────────────────────────────────────


class Crm:
    def __init__(self, records):
        self.records = records           # uuid -> (record, events)
        self.writes, self.reads, self.searches = [], [], []

    def titles(self, path):
        return {"recently sold": "tag-sold"}

    def page(self, must):
        self.searches.append(must)
        a, b = must["last_sold"]
        return [{"uuid": u, "status": r.get("status"), "list_count": len(r.get("lists") or []), "tag_count": 1,
                 "address": r.get("address")}
                for u, (r, _e) in self.records.items() if r.get("last_sold") and a <= r["last_sold"] <= b]

    def record(self, uuid):
        self.reads.append(uuid)
        return self.records[uuid][0]

    def events(self, uuid):
        return self.records[uuid][1]

    def set_status(self, uuid, value):
        self.writes.append(("status", uuid, value))
        self.records[uuid][0]["status"] = value or ""

    def untag(self, uuid):
        self.writes.append(("untag", uuid))
        self.records[uuid][0]["tags"] = []

    def tag(self, uuid):
        self.writes.append(("tag", uuid))
        self.records[uuid][0]["tags"] = [{"title": "recently sold"}]


def world():
    return Crm({
        "old-sale": (rec("2023-05-01"), list(BORN)),
        "by-hand": (rec("2023-07-04"), [created("2026-06-25", ["recently sold"]), status("2026-07-22", None, "sold", "web")]),
        "recent": (rec("2025-02-02"), list(BORN)),
    })


def ns(crm, **kw):
    base = dict(commit=False, limit=0, include_distress=False, fresh=False, force=False, stats={}, crm=crm, today=JAN)
    base.update(kw)
    return argparse.Namespace(**base)


def test_outside_january_the_pass_does_nothing_at_all():
    crm = world()
    a = ns(crm, today=date(2027, 3, 3))
    assert g.reentry(a) == 0
    assert crm.searches == [] and crm.reads == [] and crm.writes == []
    assert a.stats["reentry_due"] is False


def test_a_dry_pass_plans_and_writes_nothing():
    crm = world()
    a = ns(crm)
    assert g.reentry(a) == 0
    assert crm.writes == []
    assert a.stats["reentry_would"] == 1 and a.stats["reentered"] == 0 and a.stats["reentry_judged"] == 2
    plan = json.loads((g.OUT / "reentry_plan.json").read_text(encoding="utf-8"))
    assert {p["uuid"]: p["action"] for p in plan} == {"old-sale": "reenter", "by-hand": "leave"}
    assert not (g.OUT / "changes.jsonl").exists()


def test_a_committed_pass_resets_the_status_takes_the_tag_off_and_logs_both():
    crm = world()
    a = ns(crm, commit=True)
    assert g.reentry(a) == 0
    assert crm.writes == [("status", "old-sale", ""), ("untag", "old-sale")]
    assert a.stats["reentered"] == 1
    entry = g.read_log()[0]
    assert entry["action"] == "reenter" and entry["status_from"] == "sold" and entry["tag_removed"] is True
    assert crm.records["by-hand"][0]["status"] == "sold", "a person's Sold is untouched"


def test_a_sale_inside_the_window_is_never_even_searched_for():
    crm = world()
    g.reentry(ns(crm))
    assert "recent" not in crm.reads
    assert max(b for must in crm.searches for b in [must["last_sold"][1]]) == "2023-12-31"


def test_undo_puts_a_reentered_record_back(monkeypatch):
    crm = world()
    g.reentry(ns(crm, commit=True))
    crm.writes.clear()
    monkeypatch.setattr(g, "Crm", lambda: crm)
    g.undo(argparse.Namespace(commit=True))
    assert ("tag", "old-sale") in crm.writes and ("status", "old-sale", "sold") in crm.writes


def test_a_record_left_alone_is_not_read_again_the_next_night():
    crm = world()
    g.reentry(ns(crm, commit=True))
    crm.reads.clear()
    g.reentry(ns(crm, commit=True))
    assert "by-hand" not in crm.reads


def test_the_nightly_limit_leaves_the_rest_waiting():
    crm = world()
    a = ns(crm, limit=1)
    g.reentry(a)
    assert a.stats["reentry_judged"] == 1 and a.stats["reentry_waiting"] == 1


def test_force_runs_the_pass_outside_january():
    crm = world()
    a = ns(crm, today=date(2027, 6, 1), force=True)
    g.reentry(a)
    assert a.stats["reentry_would"] == 1


# ── the monthly audit ─────────────────────────────────────────────────


def test_the_audit_looks_at_last_month_unless_told():
    assert g.audit_month(date(2026, 11, 13)) == ("2026-10", "2026-10-01", "2026-10-31")
    assert g.audit_month(date(2027, 1, 2)) == ("2026-12", "2026-12-01", "2026-12-31")
    assert g.audit_month(date(2026, 11, 13), "2026-02") == ("2026-02", "2026-02-01", "2026-02-28")


LEAD_EVENTS = [created("2026-08-20"), joined("2026-08-20", "Foreclosure"), status("2026-10-25", "prospecting", "sold")]


def sold_lead(**kw):
    base = rec("2026-10-16", lists=["Foreclosure", "Absentee Owners"], assigned_to="user-1",
               predictivecall_attempts=0, sms_attempts=2, directmail_attempts=0)
    base.update(kw)
    return base


def test_a_sold_lead_is_described_by_how_far_we_got():
    row = g.audit_row(sold_lead(), LEAD_EVENTS)
    assert row["was_lead"] is True and row["days_as_lead"] == 57 and row["status_before"] == "prospecting"
    assert row["lists"] == "Foreclosure" and row["calls"] == 0 and row["texts"] == 2 and row["mail"] == 0
    assert row["untouched"] is False and row["assigned"] is True and row["price"] == 185000.0


def test_a_record_that_was_never_ours_is_not_counted_as_a_lost_lead():
    ev = [created("2026-06-25", ["recently sold"], "2026-05-01"), status("2026-07-22", None, "sold", "bulk")]
    row = g.audit_row(rec("2026-10-16"), ev)
    assert row["was_lead"] is False and row["verdict"] == "not a lead"


def test_the_attempt_counters_are_read_wherever_the_record_keeps_them():
    r = sold_lead(predictivecall_attempts=None, owner={"predictivecall_attempts": 3})
    assert g.audit_row(r, LEAD_EVENTS)["calls"] == 3


def test_the_summary_counts_what_was_never_done():
    rows = [g.audit_row(sold_lead(), LEAD_EVENTS),
            g.audit_row(sold_lead(sms_attempts=0, assigned_to=None), LEAD_EVENTS),
            g.audit_row(rec("2026-10-16"), [created("2026-06-25", ["recently sold"])])]
    s = g.audit_summary("2026-10", rows)
    assert s["sold_records"] == 3 and s["were_our_leads"] == 2
    assert s["never_touched"] == 1 and s["never_called"] == 2 and s["never_texted"] == 1 and s["unassigned"] == 1
    assert s["median_days_as_lead"] == 57 and s["by_list"] == {"Foreclosure": 2}
    text = g.audit_text(s)
    assert text.startswith("2026-10: 2 of our leads sold (3 sold record(s) read).")
    assert "1 were never touched, 2 never called" in text


def test_a_month_with_no_lost_lead_says_so_in_one_line():
    s = g.audit_summary("2026-10", [g.audit_row(rec("2026-10-16"), [created("2026-06-25", ["recently sold"])])])
    assert g.audit_text(s) == "2026-10: 1 record(s) we hold sold, none while it was our lead."


def audit_world():
    return Crm({"lead": (sold_lead(), list(LEAD_EVENTS)),
                "other-month": (sold_lead(last_sold="2026-09-02"), list(LEAD_EVENTS))})


def test_the_audit_reads_one_month_writes_two_files_and_changes_nothing():
    crm = audit_world()
    a = argparse.Namespace(commit=False, limit=0, month="", force=False, stats={}, crm=crm, today=date(2026, 11, 13))
    assert g.monthly_audit(a) == 0
    assert crm.writes == [], "read-only on the account"
    assert crm.reads == ["lead"]
    saved = json.loads((g.OUT / "audit_2026-10.json").read_text(encoding="utf-8"))
    assert saved["summary"]["were_our_leads"] == 1 and saved["rows"][0]["uuid"] == "lead"
    with open(g.OUT / "audit_2026-10.csv", encoding="utf-8") as fh:
        assert list(csv.DictReader(fh))[0]["street"] == "412 Sample Hollow Rd"
    assert a.stats["audit_ran"] is True


def test_the_audit_runs_once_a_month():
    crm = audit_world()
    first = argparse.Namespace(commit=False, limit=0, month="", force=False, stats={}, crm=crm, today=date(2026, 11, 13))
    g.monthly_audit(first)
    crm.reads.clear()
    crm.searches.clear()
    again = argparse.Namespace(commit=False, limit=0, month="", force=False, stats={}, crm=crm, today=date(2026, 11, 14))
    assert g.monthly_audit(again) == 0
    assert crm.searches == [] and crm.reads == [] and again.stats["audit_ran"] is False
    assert again.stats["audit_summary"]["were_our_leads"] == 1, "the stage can still report the written summary"
    forced = argparse.Namespace(commit=False, limit=0, month="", force=True, stats={}, crm=crm, today=date(2026, 11, 14))
    g.monthly_audit(forced)
    assert crm.reads == ["lead"]


# ── a new feed's list ─────────────────────────────────────────────────


def test_the_lists_the_daily_pass_treats_as_ours_are_unchanged_by_default():
    assert g.UPLOAD_LISTS == ("Foreclosure", "Probate", "Obituary", "Liens", "Condemned", "Pre-Foreclosure",
                              "Trustee Deed")


def test_a_new_feeds_list_is_added_by_a_setting(monkeypatch):
    monkeypatch.setenv("SOLD_GUARD_EXTRA_UPLOAD_LISTS", "Substitute Trustee, Tax Sale")
    try:
        mod = importlib.reload(g)
        assert mod.UPLOAD_LISTS[-2:] == ("Substitute Trustee", "Tax Sale")
        assert "Substitute Trustee" in mod.OUR_LISTS
    finally:
        monkeypatch.delenv("SOLD_GUARD_EXTRA_UPLOAD_LISTS")
        importlib.reload(g)
    assert "Substitute Trustee" not in g.OUR_LISTS


def test_the_new_commands_are_on_the_command_line():
    for cmd in ("reentry", "monthly-audit"):
        with pytest.raises(SystemExit) as exc:
            g.main([cmd, "--help"])
        assert exc.value.code == 0


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/sold_guard.py", "tests/test_sold_guard_reentry.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
