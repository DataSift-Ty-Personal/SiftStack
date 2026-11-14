"""The backlog runner for the deep prospect (src/deep_prospect_batch.py). Zero network; every
name and address is invented.

    .venv/Scripts/python.exe -m pytest tests/test_deep_prospect_batch.py -q -p no:cacheprovider
"""
import json
import sys
from datetime import date
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import deep_prospect_batch as b  # noqa: E402


def row(uuid, status="", street="12 Test Ln", first="Marla", last="Quillfeather", company=""):
    return {"uuid": uuid, "status": status, "address": {"street": street, "city": "Knoxville", "postal_code": "37900"},
            "owner": {"first_name": first, "last_name": last, "company": company}}


def test_the_gate_names_why_a_record_is_out():
    assert b.gate(row("a")) == ""
    assert b.gate(row("a", status="Sold")) == "dead status"
    assert b.gate(row("a", status="not_interested")) == "dead status"
    assert b.gate(row("a", street="")) == "no street"
    assert b.gate(row("a", first="", last="", company="Quillfeather Holdings LLC")) == "entity or unnamed owner"
    assert b.gate(row("a", first="Quillfeather Family", last="Trust")) == "entity or unnamed owner"
    assert b.gate(row("a", first="Marla", last="")) == "entity or unnamed owner"


TODAY = date(2026, 11, 12)
AHEAD = "2026-11-10\nForeclosure\nAuction: 11/20/2026"


def full(notes="", tags=(), last_sold=None, mls="Off Market", obituary=None, probate_open=None, status="",
         lists=()):
    return {"notes": notes, "tags": [{"title": t} for t in tags], "last_sold": last_sold, "mls": mls,
            "last_obituary_date": obituary, "probate_open_date": probate_open, "status": status,
            "lists": [{"title": t} for t in lists]}


class FakeCRM:
    """Answers the four calls the cohort makes: lists, tags, the records search and one full record."""

    def __init__(self, members, details=None, unreadable=()):
        self.members = members          # list title -> rows; a row may carry "_tags"
        self.details = details or {}    # uuid -> full record; a foreclosure defaults to a sale still ahead
        self.unreadable = set(unreadable)
        self.searches, self.reads = [], []

    def __call__(self, path, method="GET", body=None, headers=None):
        if path.startswith("/api/internal/list/"):
            return {"results": [{"title": t, "uuid": "L-" + t} for t in self.members]}
        if path.startswith("/api/internal/tag/"):
            return [{"title": t, "uuid": "T-" + t} for t in ("recently sold", "Deep Prospected", "Do Not Market",
                                                             "Courthouse Data")]
        if body is None:
            uuid = path.rstrip("/").rsplit("/", 1)[-1]
            self.reads.append(uuid)
            if uuid in self.unreadable:
                raise RuntimeError("502")
            on_fc = any(r["uuid"] == uuid for r in self.members.get("Foreclosure", []))
            return self.details.get(uuid) or full(notes=AHEAD if on_fc else "")
        must = body["query"]["must"]
        self.searches.append(must)
        title = must["any_lists"][0][2:]
        rows = self.members[title]
        if must.get("any_tags"):
            rows = [r for r in rows if set(must["any_tags"]) & set(r.get("_tags", []))]
        banned = set((must.get("must_not") or {}).get("any_tags") or [])
        rows = [r for r in rows if not banned & set(r.get("_tags", []))]
        off, lim = body["offset"], body["limit"]
        return {"count": len(rows), "results": rows[off:off + lim]}


def test_a_passed_auction_is_out_and_today_counts_as_passed():
    f = lambda notes, **kw: b.facts_of(full(notes=notes, **kw))  # noqa: E731
    assert b.detail_gate("Foreclosure", f("Auction: 11/20/2026"), TODAY) == ""
    assert b.detail_gate("Foreclosure", f("Auction: 11/12/2026"), TODAY) == "auction date passed"
    assert b.detail_gate("Foreclosure", f("Auction: 9/3/2026"), TODAY) == "auction date passed"
    # a postponed sale: the NEWEST date decides, in either direction
    assert b.detail_gate("Foreclosure", f("Auction: 9/3/2026\nAuction: 12/1/2026"), TODAY) == ""
    assert b.detail_gate("Foreclosure", f("Auction: 12/1/2025\nAuction: 10/1/2026"), TODAY) == "auction date passed"
    # a courthouse foreclosure we cannot date is held out, never assumed to be ahead
    assert b.detail_gate("Foreclosure", f("Foreclosure"), TODAY) == "no sale date on record"
    # the date rides with the record, so the same house is out under Probate too
    assert b.detail_gate("Probate", f("Auction: 9/3/2026"), TODAY) == "auction date passed"
    assert b.detail_gate("Probate", f(""), TODAY) == ""


def test_a_sale_after_the_notice_or_the_death_is_out_and_an_older_purchase_is_not():
    f = lambda **kw: b.facts_of(full(**kw))  # noqa: E731
    fc = dict(notes="Auction: 11/20/2026", tags=["2026-10", "knox"])
    assert b.detail_gate("Foreclosure", f(last_sold="2026-10-22", **fc), TODAY) == "sold since the notice or death"
    assert b.detail_gate("Foreclosure", f(last_sold="2024-03-01T00:00:00Z", **fc), TODAY) == ""   # the owner's own purchase
    assert b.detail_gate("Obituary", f(obituary="2026-03-05", last_sold="2026-08-01"), TODAY) \
        == "sold since the notice or death"
    assert b.detail_gate("Obituary", f(obituary="2026-03-05", last_sold="2019-08-01"), TODAY) == ""
    # probate: the earlier of the court's open date and the notice month is the line
    pr = f(probate_open="2026-06-15", tags=["2026-08"], last_sold="2026-07-01")
    assert b.ref_day("Probate", pr) == "2026-06-15"
    assert b.detail_gate("Probate", pr, TODAY) == "sold since the notice or death"
    # no notice or death date on the record: a sale in the last year still counts
    assert b.detail_gate("Obituary", f(last_sold="2026-05-01"), TODAY) == "sold since the notice or death"
    assert b.detail_gate("Obituary", f(last_sold="2025-05-01"), TODAY) == ""


def test_a_listed_house_is_out_and_an_old_mls_sale_is_not():
    f = lambda **kw: b.facts_of(full(**kw))  # noqa: E731
    assert b.detail_gate("Obituary", f(mls="Active"), TODAY) == "listed or pending on the MLS"
    assert b.detail_gate("Probate", f(mls="Pending"), TODAY) == "listed or pending on the MLS"
    assert b.detail_gate("Probate", f(mls="Listed"), TODAY) == "listed or pending on the MLS"
    # Sold on the MLS is the last sale, often the owner's own purchase months before the notice
    bought = f(mls="Sold", last_sold="2026-01-23", notes="Auction: 1/7/2027", tags=["2026-11"])
    assert b.detail_gate("Foreclosure", bought, TODAY) == ""
    assert b.detail_gate("Obituary", f(mls="Off Market"), TODAY) == ""
    assert b.detail_gate("Obituary", f(mls="Expired"), TODAY) == ""
    assert b.detail_gate("Obituary", f(mls="Sold", last_sold="2018-02-01", obituary="2026-01-01"), TODAY) == ""


def test_the_last_check_before_a_record_runs():
    passed = full(notes="Auction: 9/3/2026", lists=["Foreclosure"], tags=["Courthouse Data"])
    assert b.run_gate(passed, "Foreclosure", TODAY) == "auction date passed"
    assert b.run_gate(passed, "", TODAY, named=True) == "auction date passed"          # naming it does not revive it
    ok = full(notes=AHEAD, lists=["Foreclosure"], tags=["Courthouse Data", "Deep Prospected"])
    assert b.run_gate(ok, "Foreclosure", TODAY) == "tagged Deep Prospected"
    assert b.run_gate(ok, "Foreclosure", TODAY, redo=True) == ""
    assert b.run_gate(ok, "", TODAY, named=True) == ""
    assert b.run_gate(full(notes=AHEAD, status="Sold"), "Foreclosure", TODAY) == "dead status"
    assert b.run_gate(full(notes=AHEAD, tags=["Do Not Market"]), "Foreclosure", TODAY, redo=True) == "tagged Do Not Market"
    assert b.title_of(b.facts_of(full(lists=["Probate", "Obituary"]))) == "Probate"


def test_the_cohort_reads_each_survivor_and_drops_passed_sold_and_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "OUT", tmp_path)
    monkeypatch.setattr(b, "COHORT", tmp_path / "cohort.json")
    court = ["T-Courthouse Data"]
    crm = FakeCRM(
        {"Foreclosure": [dict(row("late"), _tags=court), dict(row("soon"), _tags=court), dict(row("gone"), _tags=court),
                         dict(row("nodate"), _tags=court), dict(row("dead", status="sold"), _tags=court)],
         "Probate": [row("gone"), row("p-sold"), row("p-ok"), row("p-err")],
         "Obituary": [row("o-listed"), row("o-ok")]},
        details={"late": full(notes="Auction: 12/15/2026"), "soon": full(notes="Auction: 11/18/2026"),
                 "gone": full(notes="Auction: 9/3/2026"), "nodate": full(notes="Foreclosure"),
                 "p-sold": full(tags=["2026-08"], last_sold="2026-10-01"), "o-listed": full(mls="Active")},
        unreadable=["p-err"])
    c = b.build_cohort(crm, today=TODAY)
    fc, pr, ob = (c["lists"][k] for k in ("Foreclosure", "Probate", "Obituary"))
    assert [r["uuid"] for r in fc["rows"]] == ["soon", "late"]                # the soonest sale first
    assert fc["rows"][0]["sale_date"] == "2026-11-18"
    assert fc["gated"]["auction date passed"] == 1 and fc["gated"]["no sale date on record"] == 1
    assert "dead" not in crm.reads                                            # no read is spent on a gated row
    assert [r["uuid"] for r in pr["rows"]] == ["p-ok"]
    assert pr["gated"]["held out under an earlier list"] == 1 and pr["gated"]["sold since the notice or death"] == 1
    assert pr["gated"]["could not read the record"] == 1
    assert [r["uuid"] for r in ob["rows"]] == ["o-ok"] and ob["gated"]["listed or pending on the MLS"] == 1
    # facts read today are reused, so a second count costs no record reads; an older read is not
    before = len(crm.reads)
    b.build_cohort(crm, today=TODAY)
    assert len(crm.reads) == before + 1                                       # only the unreadable one is retried
    assert b.load_facts(date(2026, 11, 13)) == {}


def test_the_cohort_counts_each_record_once_and_shows_every_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "OUT", tmp_path)
    monkeypatch.setattr(b, "COHORT", tmp_path / "cohort.json")
    court = ["T-Courthouse Data"]
    crm = FakeCRM({
        "Foreclosure": [dict(row("f1"), _tags=court), dict(row("f2"), _tags=court + ["T-recently sold"]),
                        dict(row("f3")),                                   # a bulk row, not courthouse
                        dict(row("both"), _tags=court)],
        "Probate": [row("p1"), row("both"), row("p2", status="sold"),
                    dict(row("p3"), _tags=["T-Deep Prospected"])],
        "Obituary": [row("o1"), row("o2", first="", last="", company="Estate Holdings LLC")],
    })
    c = b.build_cohort(crm, today=TODAY)
    fc, pr, ob = (c["lists"][k] for k in ("Foreclosure", "Probate", "Obituary"))
    assert fc["on_list"] == 3 and [r["uuid"] for r in fc["rows"]] == ["f1", "both"]
    assert fc["gated"]["tagged recently sold"] == 1
    assert [r["uuid"] for r in pr["rows"]] == ["p1"]
    assert pr["gated"] == {"tagged recently sold": 0, "tagged Deep Prospected": 1, "tagged Do Not Market": 0,
                           "already under another list": 1, "dead status": 1}
    assert [r["uuid"] for r in ob["rows"]] == ["o1"] and ob["gated"]["entity or unnamed owner"] == 1
    # exclusions ride INSIDE must: a sibling must_not is refused by the API
    assert all("must_not" not in s or isinstance(s["must_not"], dict) for s in crm.searches)
    assert json.loads((tmp_path / "cohort.json").read_text(encoding="utf-8"))["lists"]["Probate"]["in_scope"] == 1


def test_a_dry_run_never_blocks_the_live_one(tmp_path, monkeypatch):
    monkeypatch.setattr(b, "OUT", tmp_path)
    monkeypatch.setattr(b, "LOG", tmp_path / "batch_log.jsonl")
    b._log({"uuid": "a", "ok": True, "live": False})
    b._log({"uuid": "b", "ok": True, "live": True})
    b._log({"uuid": "c", "ok": False, "live": True})
    assert b.done_uuids(live=False) == {"a"}
    assert b.done_uuids(live=True) == {"b"}


def test_measured_cost_averages_live_runs_only_and_folds_engine_labels():
    m = b.measured([
        {"ok": True, "live": True, "spent_usd": 1.00, "seconds": 300,
         "phases": {"research (deepseek + muse-ember, cross-checked)": 0.30, "smartskip": 0.15, "analysis": 0.55}},
        {"ok": True, "live": True, "spent_usd": 0.60, "seconds": 500,
         "phases": {"research (deepseek + muse-ember, cross-checked)": 0.10, "smartskip": 0.15, "analysis": 0.35}},
        {"ok": True, "live": False, "spent_usd": 9.99, "phases": {"research": 9.99}},
        {"ok": False, "live": True, "spent_usd": 5.00},
    ])
    assert m["records"] == 2 and m["per_record"] == 0.8 and m["seconds"] == 400
    assert m["by_phase"] == {"analysis": 0.45, "research": 0.2, "smartskip": 0.15}


def _batch_args(**over):
    from types import SimpleNamespace
    base = dict(commit=False, uuids="u-1,u-2", addresses="", list=None, limit=0, redo=True, engine="open",
                analysis_model="", order="", daily_cap=0.0, workers=1, max_usd=5.0, force=True)
    return SimpleNamespace(**dict(base, **over))


def _fake_children(monkeypatch, tmp_path, results):
    """Stand in for the one-record child processes: nothing is launched, the environment each
    child would have had is kept."""
    from types import SimpleNamespace
    monkeypatch.setattr(b, "OUT", tmp_path)
    monkeypatch.setattr(b, "LOG", tmp_path / "batch_log.jsonl")
    seen = []

    def run(cmd, **kw):
        seen.append(kw["env"])
        return SimpleNamespace(stdout="RESULT " + json.dumps(results[cmd[-1]]), stderr="", returncode=0)

    monkeypatch.setattr(b.subprocess, "run", run)
    return seen


def test_the_order_of_the_chain_is_passed_to_each_record_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTS_DP_ORDER", raising=False)
    done = {"u-1": {"uuid": "u-1", "ok": True, "street": "12 Test Ln", "owner_status": "Living", "spent_usd": 1.0},
            "u-2": {"uuid": "u-2", "ok": True, "street": "14 Test Ln", "owner_status": "Living", "spent_usd": 1.0}}
    seen = _fake_children(monkeypatch, tmp_path, done)
    assert b.run_batch(_batch_args(order="free_first")) == 0
    assert [e["AGENTS_DP_ORDER"] for e in seen] == ["free_first", "free_first"]
    seen.clear()
    assert b.run_batch(_batch_args()) == 0
    assert all("AGENTS_DP_ORDER" not in e for e in seen)          # unset: today's order, or whatever the box says


def test_a_record_the_free_look_stopped_is_shown_as_stopped_with_the_reason(tmp_path, monkeypatch, capsys):
    why = "The deed records show the owner no longer owns this house."
    seen = _fake_children(monkeypatch, tmp_path, {
        "u-1": {"uuid": "u-1", "ok": True, "street": "12 Test Ln", "owner_status": "", "stopped": why, "spent_usd": 0.0},
        "u-2": {"uuid": "u-2", "ok": True, "street": "14 Test Ln", "owner_status": "Living", "decision_maker": "Marla Quillfeather",
                "spent_usd": 1.2, "written_back": True}})
    assert b.run_batch(_batch_args(order="free_first")) == 0 and len(seen) == 2
    out = capsys.readouterr().out
    assert "STOPPED" in out and why[:60] in out
    assert "Done: 2 of 2 finished, 1 written back, $1.20 spent" in out


def test_the_module_holds_no_long_dash_or_backspace_byte():
    text = (SRC / "deep_prospect_batch.py").read_text(encoding="utf-8")
    assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text
