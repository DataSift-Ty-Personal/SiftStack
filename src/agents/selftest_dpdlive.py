"""Live Doors per Deal selftest (Ty, 2026-11-24: "as real-time data as we possibly can using the sold
property data and the API ... pair that with the framework to use for the top 1,000"): the dpd_live
stage on a fake SiftMap and fake published ladder files, the list factors the Top 1,000 reads, and the
Top 1,000's house flags rebuilt by filter now that SiftMap's search rows no longer carry them.

Zero network. Every county, id and street here is invented.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import config
from .selftest_autopilot import EMPLOYEES, ET, FakeCRM

NOW = datetime(2026, 11, 24, 3, 40, tzinfo=ET).astimezone(timezone.utc)
ALDER = {"fips": "99001", "name": "Alder", "state": "ZZ"}
PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Emerson"]},
    "stages": {"dpd_live": {"kind": "dpdlive", "owner": "analysis", "mode": "live", "schedule": "03:40",
                            "counties": [dict(ALDER)], "max_searches": 400, "top": 3}},
}


def houses() -> list[dict]:
    out = []
    for i in range(1, 61):
        flags = set()
        if i <= 5:
            flags.add("is_notice_of_default")
        if 3 <= i <= 20:
            flags.add("is_recent_deliquent")
        if 30 <= i <= 59:
            flags.add("is_absentee_owners")
        out.append({"id": "a%d" % i, "uuid": "rec-a%d" % i, "flags": flags, "value": 100000 + i, "sold": None,
                    "saved": i % 3 == 0})
    for i, m in zip(range(40, 46), ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]):
        out[i - 1]["sold"] = m
    for i, m in zip(range(46, 53), ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-09", "2026-10"]):
        out[i - 1]["sold"] = m
    return out


class FakeMap:
    """SiftMap's search over invented Alder: the ladder's filters, the sold-property window, the in-account
    mode, paging by row offset. Its rows carry no flags, as SiftMap's have since November 2026."""

    def __init__(self, hs, rows_carry_flags=False):
        self.hs, self.flags_on_rows, self.bodies = hs, rows_carry_flags, []

    def match(self, h, f):
        for k, v in f.items():
            if k == "distressors_include" and not set(v.split(",")) <= h["flags"]:
                return False
            if k == "preset_absentee_owners" and "is_absentee_owners" not in h["flags"]:
                return False
            if k == "extra_last_sale_investor_transaction_type" and not h["sold"]:
                return False
            if k == "extra_last_sale_date_min" and (not h["sold"] or h["sold"] < v[:7]):
                return False
            if k == "extra_last_sale_date_max" and (not h["sold"] or h["sold"] > v[:7]):
                return False
            if k == "in_my_account_mode" and v == "in" and not h["saved"]:
                return False
            if k == "value_min" and h["value"] < v:
                return False
            if k == "value_max" and h["value"] > v:
                return False
        return True

    def __call__(self, crm, body):
        self.bodies.append(copy.deepcopy(body))
        assert body["addresses"][0]["search"] == "Alder", "the county is searched by its bare name"
        hits = [h for h in self.hs if self.match(h, body.get("filters") or {})]
        start, size = int(body.get("result_index") or 1) - 1, int(body.get("size") or 25)
        rows = []
        for h in hits[start:start + size]:
            row = {"id": h["id"], "address": "%s Sample St, Alderton, ZZ 99001" % h["id"][1:], "estimatedValue": h["value"],
                   "equityPercent": 40, "saved": h["saved"], "saved_uuid": h["uuid"] if h["saved"] else None,
                   "saved_tags": [], "saved_lists": [], "propertyUse": "Residential (General) (Single)"}
            if self.flags_on_rows:
                row["distressors"] = {k: {"is_active": True} for k in h["flags"]}
            rows.append(row)
        return {"total_results": len(hits), "data": rows}


def published(url):
    rungs = [
        {"label": "Notice of Default", "keys": ["is_notice_of_default"], "ai": None, "priority": 1, "list_size": 4,
         "marg_deals": 1, "marg_supply": 4, "marg_dpd": 4.0, "cum_dpd": 4.0},
        {"label": "Tax Delinquent", "keys": ["is_recent_deliquent"], "ai": None, "priority": 2, "list_size": 9,
         "marg_deals": 1, "marg_supply": 7, "marg_dpd": 7.0, "cum_dpd": 5.5},
        {"label": "Absentee", "keys": ["is_absentee_owners"], "ai": None, "priority": 3, "list_size": 30,
         "marg_deals": 2, "marg_supply": 30, "marg_dpd": 15.0, "cum_dpd": 10.3}]
    if url.endswith("capture-data/99.json"):
        return {"99001": {"fips": "99001", "name": "Alder", "st": "ZZ", "deals": 9, "base_supply": 60, "ai_provisioned": False,
                          "window": "2026-01 .. 2026-06 (6 complete months)", "built": "2026-08-13",
                          "variants": {"lists": rungs}}}
    if url.endswith("county-data/99.json"):
        return {"99001": {"name": "Alder", "state": "ZZ", "window": "2025-12 .. 2026-05 (6 complete months)",
                          "rows": [{"seg": "Tax Delinquent", "keys": ["is_recent_deliquent"], "deals": 2, "list_size": 9}],
                          "ai_demand": {"cutoffs": []}}}
    raise AssertionError("unexpected file %s" % url)


class Out:
    def __init__(self):
        self.tally, self.problems = Counter(), []

    def problem(self, text):
        self.problems.append(text)


def run(check, tmp: Path) -> None:
    import dpd_live
    from . import store
    from .autopilot import dpdlive, laddercheck, policy, registry, sweep, top1000
    saved = (config.DATA_DIR, laddercheck.search, dpd_live.fetch_json, top1000.search, dict(top1000._ODDS))
    config.DATA_DIR = Path(tmp) / "dpdlive"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    store.init()
    dpd_live._SHARDS.clear()
    try:
        _stage(check, store, dpd_live, dpdlive, laddercheck, policy, registry, sweep)
        _factors(check, dpd_live, dpdlive, top1000)
        _rebuild(check, top1000)
    finally:
        config.DATA_DIR, laddercheck.search, dpd_live.fetch_json, top1000.search = saved[:4]
        top1000._ODDS.clear()
        top1000._ODDS.update(saved[4])
        dpd_live._SHARDS.clear()
        config.MODE = "dry"


def _stage(check, store, dpd_live, dpdlive, laddercheck, policy, registry, sweep) -> None:
    print("  the live ladder stage")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    check(registry.EXTRA.get("dpdlive") == ("dpdlive", "run_dpdlive", "free", False, True)
          and pol.stage("dpd_live").spend == "free", "the stage is registered as a free report: it reads, it never spends")
    bad = copy.deepcopy(PLAYBOOK)
    bad["stages"]["dpd_live"]["counties"] = [{"name": "Alder"}]
    try:
        policy.parse(bad, EMPLOYEES)
        check(False, "a county with no fips is refused when the playbook loads")
    except Exception as exc:  # noqa: BLE001
        check("5-digit fips" in str(exc), "a county with no fips is refused when the playbook loads: %s" % str(exc)[:120])
    fake = FakeMap(houses())
    laddercheck.search, dpd_live.fetch_json = fake, published
    crm = FakeCRM([])
    s = sweep.run("dpd_live", crm=crm, now=NOW, pol=pol, plan=True)
    text = "\n".join(s.lines)
    folder = Path(config.DATA_DIR) / "dpd_live" / "99001"
    ladder = json.loads((folder / "ladder.json").read_text(encoding="utf-8"))
    check(crm.writes == [] and s.writes == 0, "the stage writes nothing to the account")
    check("Alder: investors bought" in text and "Alder, best lists now: Notice of Default 1 in" in text,
          "the run says the market's speed and the best lists in plain words: %s" % s.lines[:2])
    check(ladder["snapshot"]["date"] == "2026-11-24" and ladder["snapshot"]["houses"] == 5 + 18 + 30,
          "the first run of the month saves every list's members: %s" % ladder.get("snapshot"))
    check(all(b["filters"].get("type_single_family") for b in fake.bodies),
          "every search is single family, the published ladder's own basis")
    check(not any(b["filters"].get("extra_last_sale_date_min", "0000")[:7] >= "2026-10" for b in fake.bodies),
          "the month still being recorded is never read")
    kept = json.loads(store.kv_get("autopilot:dpd_live") or "{}")
    check(kept.get("99001", {}).get("best") and kept["99001"]["snapshot"] == "2026-11-24",
          "the best lists are kept for the team's queries")
    code = Path(dpd_live.__file__).read_text(encoding="utf-8")
    check("add-properties" not in code and code.count('method="POST"') == 1 and "MAP + SEARCH_PATH" in code
          and all(v not in code for v in ('"PATCH"', '"PUT"', '"DELETE"')),
          "the only thing the engine can post to is the SiftMap search")
    # A second run the same month reads no members again.
    before = len(fake.bodies)
    sweep.run("dpd_live", crm=crm, now=NOW, pol=pol, plan=True)
    check(len(fake.bodies) - before < 12, "a later run the same month counts the lists and reads no members again (%d searches)"
          % (len(fake.bodies) - before))


def _factors(check, dpd_live, dpdlive, top1000) -> None:
    print("  the Top 1,000's list factors")
    folder = Path(config.DATA_DIR) / "dpd_live" / "99001"
    f = dpdlive.list_factors(["99001"])
    check(f.get("a1") == (1.0, "Notice of Default") and f.get("a7") == (1.0, "Tax Delinquent") and f.get("a31") == (1.0, "Absentee"),
          "a house sits on the first list of the published order that holds it, and every factor is 1 until a list "
          "has sales of its own: %s" % {k: f.get(k) for k in ("a1", "a7", "a31")})
    ladder = json.loads((folder / "ladder.json").read_text(encoding="utf-8"))
    ratio = float(ladder["speed"]["ratio"])
    for r in ladder["rungs"]:
        if r["label"] == "Tax Delinquent":
            r["rate"] = 2 * r["rate_published"] * ratio            # selling twice as well as published, measured
    (folder / "ladder.json").write_text(json.dumps(ladder), encoding="utf-8")
    f2 = dpdlive.list_factors(["99001"])
    check(f2.get("a7") == (2.0, "Tax Delinquent") and f2.get("a1") == (1.0, "Notice of Default"),
          "a list that sells better than published lifts its own houses, and only them")
    top1000._ODDS["live"] = {"d-x": (1.5, "Tax Delinquent")}
    r = {}
    check(abs(top1000.live_odds(0.2, {"id": "d-x"}, r) - 0.3) < 1e-9 and r["list_now"] == {"list": "Tax Delinquent", "factor": 1.5},
          "a house's odds are times its list's factor, and the list is kept on the house")
    check(top1000.live_odds(0.9, {"id": "d-x"}, {}) == 0.99 and top1000.live_odds(0.2, {"id": "d-y"}, {}) == 0.2
          and top1000.live_odds(0.2, None, {}) == 0.2,
          "a factor never makes a sale certain, and a house on no saved list keeps its odds")
    top1000._ODDS["live"] = {}
    st = SimpleNamespace(id="top1000_refresh", get=lambda k, d=None: {"live_lists": "yes", "counties": [ALDER]}.get(k, d),
                         params={})
    check(any("live_lists is true or false" in p for p in top1000.block_problems(st)),
          "live_lists must be true or false in the playbook")


def _rebuild(check, top1000) -> None:
    print("  the Top 1,000's flags rebuilt by filter")
    hs = houses()
    fake = FakeMap(hs)
    top1000.search = fake
    ctx = SimpleNamespace(crm=SimpleNamespace(tag_uuid=lambda t: None), out=Out(), now=NOW,
                          policy=SimpleNamespace(tz="America/New_York", gate=None))
    o = top1000.options({"counties": [dict(ALDER)], "max_searches": 200, "page_size": 100})
    rows = top1000.month_sweep(ctx, o)
    flagged = {u: r["flags"] for u, r in (rows or {}).items() if r.get("flags")}
    check(rows is not None and len(rows) == 20, "the sweep reads every house in the account: %s" % (len(rows or {})))
    check(flagged.get("rec-a3") == ["is_notice_of_default", "is_recent_deliquent"] and flagged.get("rec-a6") == ["is_recent_deliquent"]
          and "rec-a30" not in flagged,
          "with no flags on the rows, each flag the model reads is searched and marked on its members (absentee is not one "
          "the model learns from): %s" % {k: flagged.get(k) for k in ("rec-a3", "rec-a6", "rec-a30")})
    check(ctx.out.tally["house flags rebuilt by filter (SiftMap rows no longer carry them)"] == 7,
          "the rebuild is said in the run's tally: %s" % dict(ctx.out.tally))
    # Rows that carry their flags again need no extra searches.
    ctx2 = SimpleNamespace(crm=ctx.crm, out=Out(), now=NOW.replace(month=12), policy=ctx.policy)
    fake2 = FakeMap(hs, rows_carry_flags=True)
    top1000.search = fake2
    rows2 = top1000.month_sweep(ctx2, o)
    check(rows2 is not None and not any("distressors_include" in (b.get("filters") or {}) for b in fake2.bodies)
          and rows2["rec-a3"]["flags"] == ["is_notice_of_default", "is_recent_deliquent"],
          "when SiftMap's rows carry their flags, they are read off the rows and nothing is searched again")
