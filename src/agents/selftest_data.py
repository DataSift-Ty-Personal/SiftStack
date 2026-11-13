"""Data, lists and hygiene selftest (build plan phase 5): the monthly ladder check and the weekly
conventions check, against a fake DataSift, a fake SiftMap search, fake published ladder files and
a fake feeder listing. Zero network. Every county, list, tag, street and number here is invented.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, ET, PRESETS, FakeCRM, _rec

NOW = datetime(2026, 11, 11, 3, 10, tzinfo=ET).astimezone(timezone.utc)     # a night run in November
ALDER = {"fips": "99001", "name": "Alder", "state": "ZZ"}
# The two kinds this section covers, as they are registered (autopilot/registry.py EXTRA).
KINDS = {"laddercheck": ("laddercheck", "run_laddercheck", "free", False, True),
         "conventions": ("conventions", "run_conventions", "free", False, True)}

PLAYBOOK = {
    "business": {"tz": "America/New_York", "hours": "09:00-18:00", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
    "caps": {"daily_usd": 25, "record_usd": 1.0},
    "people": {"callers": ["Adriana"]},
    "stages": {
        "ladder_check": {"kind": "laddercheck", "owner": "analysis", "mode": "plan", "schedule": "1st 03:10",
                         "counties": [dict(ALDER)], "months": 6, "lag_months": 1, "max_searches": 60,
                         "reached_preset": "Reached - Confirmed Number"},
        "conventions_check": {"kind": "conventions", "owner": "analysis", "mode": "plan", "schedule": "Sun 03:40",
                              "caller_folders": ["05. FTM - CALL"]},
    },
}


# ------------------------------------------------------------------------------------ the fakes
class DataCRM(FakeCRM):
    """The autopilot's fake DataSift, with the listings these two stages read."""

    def __init__(self, records=(), lists=(), system_lists=(), tags=(), user=None, folders=None, fields=None):
        super().__init__(list(records))
        self.lists_, self.system_, self.tags_ = list(lists), list(system_lists), list(tags)
        self.user_ = {"auto_update": True, "update_lists": True} if user is None else user
        self.folders_ = folders if folders is not None else {}
        self.fields = dict(fields if fields is not None else {"inbound source": "f-inbound"})
        self.asked, self.broken = [], ()

    def call(self, path, method="GET", body=None, headers=None):
        assert method == "GET" and body is None, "these stages only read: %s %s" % (method, path)
        self.asked.append(path)
        if any(path.startswith(b) for b in self.broken):
            raise RuntimeError("HTTP 500 on GET %s" % path)
        if path.startswith("/api/internal/list/"):
            return {"results": copy.deepcopy(self.lists_)}
        if path.startswith("/api/internal/system-list/"):
            return {"results": copy.deepcopy(self.system_)}
        if path.startswith("/api/internal/tag/"):
            return {"results": copy.deepcopy(self.tags_)}
        if path.startswith("/api/internal/user/"):
            return dict(self.user_)
        folders = "/api/internal/filter-preset-folder/"
        if path.startswith(folders + "?"):
            return {"results": [{"uuid": "fold%d" % i, "title": t} for i, t in enumerate(self.folders_)]}
        if path.startswith(folders):
            i = int(path[len(folders):].split("/")[0].replace("fold", ""))
            assert "limit=999" in path, "a folder listing shows ten presets unless it is asked for more"
            return {"results": copy.deepcopy(list(self.folders_.values())[i])}
        return super().call(path, method, body, headers)

    def _match(self, r, must):
        window = must.get("last_direct_mailed")
        if window is not None:
            day = r.get("last_direct_mailed") or ""
            if not (day and window[0] <= day < window[1]):
                return False
        return super()._match(r, must)

    def count(self, must):
        self.asked.append("count:%s" % ",".join(sorted(k for k in must if not k.startswith("_"))))
        return super().count(must)


def lst(title, size=None, uuid=None):
    return {"uuid": uuid or "list-" + title.lower(), "title": title, "properties_count": None if size is None else str(size)}


def tag(title, size=0):
    return {"uuid": "tag-" + title.lower(), "title": title, "properties_count": str(size)}


class Sold:
    """A fake SiftMap sold index. It honors the filters the published reference lists and, like
    the real one, answers a key it does not know as if the key were not there."""

    def __init__(self, rows, page=250, drift=0):
        self.rows, self.bodies, self.ignore_date_max, self.no_account_fields = list(rows), [], False, False
        self.page, self.drift, self.no_lists = page, drift, False

    def __call__(self, crm, body):
        self.bodies.append(copy.deepcopy(body))
        f = body["filters"]
        county = body["addresses"][0]["county"]
        assert body["addresses"][0]["search"] == county, "the county is searched by its bare name"
        hit = []
        for r in self.rows:
            if r["county"] != county:
                continue
            if "extra_last_sale_investor_transaction_type" in f:
                if r.get("type", "pending") not in f["extra_last_sale_investor_transaction_type"]:
                    continue
                if "extra_last_sale_date_min" in f and r["sold"] < f["extra_last_sale_date_min"]:
                    continue
                if "extra_last_sale_date_max" in f and not self.ignore_date_max and r["sold"] > f["extra_last_sale_date_max"]:
                    continue
            if "value_max" in f and r.get("value", 0) > f["value_max"]:
                continue
            if "extra_year_built_max" in f and r.get("year", 0) > f["extra_year_built_max"]:
                continue
            hit.append(r)
        start = body["result_index"] - 1
        page = []
        whole_window = f.get("extra_last_sale_date_min", "")[:7] != f.get("extra_last_sale_date_max", "")[:7]
        for r in hit[start:start + self.page]:
            row = {"id": r["id"], "address": "%s Sold St, Alderton, ZZ" % r["id"], "estimatedValue": r.get("value", 0)}
            if not self.no_account_fields:
                row.update(saved=bool(r.get("saved")), saved_uuid=("rec-" + r["id"]) if r.get("saved") else None,
                           saved_date=(r.get("saved") + "T12:00:00Z") if r.get("saved") else None,
                           saved_lists=list(r.get("lists") or []) if r.get("saved") and not self.no_lists else None,
                           saved_tags=None)
            page.append(row)
        return {"result_index": start, "total_results": len(hit) + (self.drift if whole_window else 0), "data": page}


def sold_rows():
    """Six recorded months in Alder. Each of the first five holds the same mix; September is short."""
    months = [(4, 40), (5, 44), (6, 38), (7, 41), (8, 45), (9, 4)]
    rows = []
    for m, n in months:
        first = date(2026, m, 1)

        def before(days):
            return (first - timedelta(days=days)).isoformat()

        for i in range(n):
            r = {"id": "s%02d%03d" % (m, i), "county": "Alder", "sold": (first + timedelta(days=i % 27)).isoformat(),
                 "value": 180000, "year": 1975}
            if m == 9:
                r.update(saved=before(200), lists=["list-tax delinquent"])             # the short month: never counted
            elif i < 4:
                r.update(saved=before(200), lists=["list-tax delinquent"])
            elif i < 6:
                r.update(saved=before(100), lists=["list-tax delinquent", "list-vacant"])
            elif i == 6:
                r.update(saved=before(30), lists=["sys-absentee"])
            elif i == 7:
                r.update(saved=(first + timedelta(days=5)).isoformat(), lists=["list-dispo - flip buyers"])   # added after it sold
            elif i == 8:
                r.update(saved=before(400), lists=["list-dispo - flip buyers"])
            elif i == 9:
                r.update(saved=before(50), lists=["list-gone"])
            rows.append(r)
    # sales the stage must never ask for: the month still being recorded, and another county
    rows.append({"id": "oct1", "county": "Alder", "sold": "2026-10-09", "saved": "2026-01-01", "lists": ["list-tax delinquent"],
                 "value": 180000, "year": 1975})
    rows.append({"id": "b1", "county": "Birch", "sold": "2026-06-09", "saved": "2026-01-01", "lists": ["list-tax delinquent"],
                 "value": 180000, "year": 1975})
    return rows


def account_lists():
    return [lst("Tax Delinquent", 600), lst("Vacant", 400), lst("Foreclosure", 120), lst("Dispo - Flip Buyers", 2000),
            lst("Probate", 60), lst("DNC", 30)]


def published(url):
    """The two published files for the invented state 99."""
    def rung(label, keys, dpd, priority=1, ai=None, thin=False):
        return {"label": label, "keys": keys, "ai": ai, "priority": priority, "thin": thin, "est": False,
                "list_size": 400, "marg_supply": 400, "marg_dpd": dpd}
    if url.endswith("capture-data/99.json"):
        return {"99001": {"fips": "99001", "name": "Alder", "variants": {"ai": [
            rung("Notice of Default", ["is_notice_of_default"], 11.0, thin=True),
            rung("AI Score 90+", [], 22.6, ai=90),
            rung("Tax Delinquent + Vacant", ["is_recent_deliquent", "is_vacant_property"], 27.5),
            rung("Tired Landlord", ["is_tired_landlord"], 31.0),
            rung("AI Score 70+", [], 60.0, priority=2, ai=70)]}}}
    if url.endswith("county-data/99.json"):
        def county(name, trend):
            return {"name": name, "state": "ZZ", "window": "2026-01 .. 2026-06 (6 complete months)", "deals": sum(trend),
                    "trend": trend, "baseline": {"deals": sum(trend), "list_size": 90000, "dpd": 150.0}, "rows": []}
        return {"99001": county("Alder", [150, 160, 155, 170, 148, 0]), "99003": county("Birch", [40, 44, 38, 41, 45, 0])}
    raise AssertionError("unexpected file %s" % url)


def feeder_rows(crm=None):
    def row(i, name, filters, tags=(), auto=True, description=None):
        return {"id": i, "name": name, "description": description, "is_active": True, "scope": "account",
                "auto_add_enabled": auto, "replace_owners_enabled": False, "tags": list(tags) or None, "lists": None,
                "updated": "2026-10-01T10:00:00Z",
                "filter_data": {"filters": dict(filters), "addresses": [{"county": "Alder", "searchType": "county"}]}}
    box = {"value_max": 700000, "value_min": 100000, "type_single_family": True}
    return [row(20, "Alder - Priority 1 - AI 90+", dict(box, investor_off_market_score_min=90), tags=("Priority 1",)),
            row(21, "Alder - Priority 1 - Tired Landlord", dict(box, distressors_include="is_tired_landlord"),
                tags=("Priority 1",), auto=False)]


def _register(registry, policy, sweep):
    """Register the two kinds for this section when the integration has not yet, and hand back the
    function that puts everything as it was. The rows are the ones proposed for registry.EXTRA."""
    saved = (policy.KINDS, policy.NO_PRESETS, sweep.REPORT_KINDS, dict(policy.SPEND_BY_KIND))
    added = [k for k in KINDS if k not in registry.EXTRA]
    for k in added:
        registry.EXTRA[k] = KINDS[k]
    policy.KINDS = tuple(dict.fromkeys(tuple(policy.KINDS) + tuple(KINDS)))
    policy.NO_PRESETS = tuple(dict.fromkeys(tuple(policy.NO_PRESETS) + tuple(k for k, r in KINDS.items() if not r[3])))
    policy.SPEND_BY_KIND.update({k: r[2] for k, r in KINDS.items()})
    sweep.REPORT_KINDS = set(sweep.REPORT_KINDS) | {k for k, r in KINDS.items() if r[4]}

    def restore():
        for k in added:
            registry.EXTRA.pop(k, None)
        policy.KINDS, policy.NO_PRESETS, sweep.REPORT_KINDS = saved[0], saved[1], saved[2]
        policy.SPEND_BY_KIND.clear()
        policy.SPEND_BY_KIND.update(saved[3])
    return restore


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import conventions, laddercheck, policy, registry, sweep
    config.DATA_DIR = tmp
    store.init()
    restore = _register(registry, policy, sweep)
    saved_presets = dict(PRESETS)
    PRESETS["Reached - Confirmed Number"] = lambda r: bool(r.get("reached"))
    PRESETS["Reached - On Lists"] = lambda r: bool(r.get("reached"))
    hooks = (laddercheck.search, laddercheck.fetch_json, laddercheck.read_feeders)
    try:
        with store._LOCK, store._db() as c:
            c.execute("DELETE FROM kv WHERE key LIKE 'autopilot:ladder%' OR key LIKE 'autopilot:conventions%'")
        _ladder(check, store, laddercheck, policy, registry, sweep)
        _conventions(check, store, conventions, policy, sweep)
    finally:
        laddercheck.search, laddercheck.fetch_json, laddercheck.read_feeders = hooks
        PRESETS.clear()
        PRESETS.update(saved_presets)
        restore()
        config.MODE = "dry"


# ------------------------------------------------------------------------------------ the ladder check
def _ladder_crm():
    recs = []
    for i in range(12):
        r = _rec("t%d" % i, "%d Ladder St" % (10 + i))
        r["lists"] = ["Tax Delinquent"] + (["Vacant"] if i < 5 else [])
        r["reached"] = i in (0, 1, 2, 7)
        recs.append(r)
    return DataCRM(recs, lists=account_lists(), system_lists=[lst("Absentee Owners", 5000, uuid="sys-absentee")])


def _ladder(check, store, laddercheck, policy, registry, sweep) -> None:
    print("  ladder check")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)
    check(registry.problems() == [] and all(registry.EXTRA[k] == row for k, row in KINDS.items()),
          "both kinds load from the registry rows this section proposes")
    check(pol.stage("ladder_check").spend == "free" and pol.stage("conventions_check").spend == "free",
          "neither stage can spend: both are free kinds")

    months = laddercheck.recorded_months(date(2026, 11, 11), 6, 1)
    check([m[0] for m in months] == ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"],
          "six whole months are read, and the newest one is skipped while its deeds are still being recorded")
    check(months[0][1:] == (date(2026, 4, 1), date(2026, 4, 30)) and months[-1][2] == date(2026, 9, 30),
          "each month runs from its first day to its last")
    check([m[0] for m in laddercheck.recorded_months(date(2026, 2, 10), 3, 1)] == ["2025-10", "2025-11", "2025-12"]
          and laddercheck.recorded_months(date(2024, 4, 2), 1, 1)[0][2] == date(2024, 2, 29),
          "the months roll back across a year end, and a leap February ends on the 29th")

    sold = Sold(sold_rows())
    laddercheck.search, laddercheck.fetch_json, laddercheck.read_feeders = sold, published, feeder_rows
    crm = _ladder_crm()
    s = sweep.run("ladder_check", crm=crm, now=NOW, pol=pol, plan=True)
    text = "\n".join(s.lines)
    check(crm.writes == [] and s.writes == 0 and not s.live, "the ladder check writes nothing, to the account or anywhere a homeowner is")
    check(not any(b["filters"].get("extra_last_sale_date_min", "") >= "2026-10-01" for b in sold.bodies),
          "the month still being recorded is never asked for")
    check(all(b["filters"]["extra_last_sale_investor_transaction_type"] == laddercheck.INVESTOR_TYPES and b["filters"]["type_single_family"]
              for b in sold.bodies if "extra_last_sale_date_min" in b["filters"]),
          "every sold search asks for investor purchases of single family houses")
    check("Alder: 208 investor deal(s) in 5 complete months, September left out: it reads 4, far below the 41 a month around it." in text,
          "a month far below the months around it is left out, said, and its deals are not counted: %s" % s.lines[:3])
    check("45 (21.6%) were records here before they sold; 5 were added after." in text,
          "a record counts only when it was in the account before the month it sold")
    check("Alder, share of deals by list: Tax Delinquent 14.4% (30); Vacant 4.8% (10); Absentee Owners 2.4% (5) [follows today's owner]." in text,
          "each list's share of the deals is said, and a list DataSift keeps current is marked as following today's owner")
    check("Dispo" not in text, "a buyer list is never read as a seller list: it holds every investor purchase by construction")
    check(s.tally["list marks on sold records that no longer match a list"] == 5, "a list the account no longer has is counted, not guessed at")
    check("Alder, this account's own doors per deal: Tax Delinquent 1 in 17, 7 months to a sale; Vacant 1 in 33, 3.7 months to a sale; "
          "Absentee Owners 1 in 833, 1.4 months to a sale." in text,
          "a list is proven on the account's own sales: doors per deal at a six-month rate, and the months a record waited")
    check("Alder, reach: Tax Delinquent 4 of 600 reached (0.7%), about 0.2 deal(s) in six months; Vacant 3 of 400 reached (0.8%), "
          "about 0.1 deal(s) in six months." in text,
          "reach is read through the named preset, and reached over doors per deal is the deals that reach should produce")
    check("Alder, published doors per deal: 5 complete months, June left out: it reads zero. Read every figure at 0.83 of its size." in text,
          "the published file goes through the window guard, and the month it cannot stand behind is named")
    check("4 Priority 1 rung(s) on the ladder: 1 with a feeder adding records, 1 with none, 1 saved but not adding, "
          "1 small sample (county records are the source)." in text,
          "the Priority 1 rungs are compared with the feeders: covered, missing, saved with auto-add off, and small sample")
    check(any(p == "1 Priority 1 rung(s) have no feeder: Tax Delinquent + Vacant" for p in s.problems),
          "a rung with no feeder is named: %s" % s.problems)
    check(any("Rung 2, [20] Alder - Priority 1 - AI 90+: no built-by-2010 filter; no owned-7-years filter" in ln for ln in s.lines),
          "a feeder without the base filters is named with what it lacks")
    check(s.found == 208 and s.acted == 4 and s.tally["SiftMap searches"] == 7,
          "one search a month and one for the whole window: seven for a county this size (%s, %s, %s)" % (s.found, s.acted, dict(s.tally)))
    check(s.head == "Ladder check for Alder, April to September 2026: 208 investor deal(s) read, 5 thing(s) to look at.",
          "the headline says the counties, the months read, the deals and how many things need a look: %s" % s.head)
    kept = json.loads(store.kv_get("autopilot:ladder:2026-11"))
    check(kept["counties"]["Alder"]["deals"] == 208 and kept["counties"]["Alder"]["counts"]["2026-09"] == 4
          and kept["counties"]["Alder"]["lists"][0]["doors_per_deal"] == 16.7 and kept["feeders"]["summary"]["missing"] == 1,
          "the month's numbers are kept as the autopilot's own record")
    said = sweep.slack_text({"workflow": "ladder_check", "source": "schedule"}, s.as_dict(), now=NOW)
    check(said.startswith(":warning: *ladder check* _(plan: nothing written)_ Ladder check for Alder"), "the monthly report is posted: %s" % said[:80])
    check(not any(ch.isdigit() and "St" in ln for ln in s.lines for ch in ln if " St," in ln) and "Sold St" not in text + s.head,
          "no street or owner appears in the report: lists, counties and counts only")

    # the same account a month later: list sizes are compared with what was remembered
    crm2 = _ladder_crm()
    crm2.lists_[0] = lst("Tax Delinquent", 660)
    s2 = sweep.run("ladder_check", crm=crm2, now=NOW + timedelta(days=30), pol=pol, plan=True)
    check(any(ln == "List sizes since 2026-11: Tax Delinquent +60 (10%)." for ln in s2.lines),
          "a list that grew or shrank by a twentieth or more since last month is said: %s" % [ln for ln in s2.lines if "sizes" in ln])
    check(json.loads(store.kv_get("autopilot:ladder:sizes"))["month"] == "2026-12", "and the new sizes are what next month compares with")

    # every count is proven before it is used
    sold_bad = Sold(sold_rows())
    sold_bad.ignore_date_max = True
    laddercheck.search = sold_bad
    b = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(any("so the date filter cannot be trusted. Nothing is reported for the county" in p for p in b.problems)
          and not any("share of deals" in ln or "investor deal(s) in" in ln for ln in b.lines),
          "when the months do not add up to the window, the date filter is being ignored and the county is not reported")
    laddercheck.search = Sold(sold_rows(), drift=1)
    d1 = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(any(ln.startswith("Alder: 208 investor deal(s)") for ln in d1.lines) and not any("date filter" in p for p in d1.problems),
          "one deed recorded between two requests does not refuse a county: an ignored filter is off by months, not by one")
    small = Sold(sold_rows(), page=10)
    laddercheck.search = small
    pg = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(pg.found == 208 and pg.tally["SiftMap searches"] == 25 and all(b["result_index"] % 10 == 1 for b in small.bodies),
          "a month is read page after page to its end, each page starting after the rows already read (%s searches)"
          % pg.tally["SiftMap searches"])
    plain = Sold(sold_rows())
    plain.no_account_fields = True
    laddercheck.search = plain
    n = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(any("carried no account fields" in p for p in n.problems) and not any("share of deals" in ln for ln in n.lines),
          "sold rows with no account fields are reported as unmeasured, never as zero deals on every list")
    untold = Sold(sold_rows())
    untold.no_lists = True
    laddercheck.search = untold
    u = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(any("did not say which lists each record is on" in p for p in u.problems)
          and any("45 (21.6%) were records here before they sold" in ln for ln in u.lines)
          and not any("share of deals" in ln or "none of the deals" in ln for ln in u.lines),
          "a row that does not say its lists is not read as a record on no list")

    laddercheck.search = Sold(sold_rows())
    wide = policy.parse(_with(PLAYBOOK, "ladder_check", reached_preset="Reached - On Lists"), EMPLOYEES)
    crm_w = _ladder_crm()
    crm_w.preset_must = lambda title: {"_preset": title, "any_lists": ["list-vacant"]}
    w = sweep.run("ladder_check", crm=crm_w, now=NOW, pol=wide, plan=True)
    check(any("names lists of its own" in p for p in w.problems) and not any("reach:" in ln for ln in w.lines),
          "a reached preset that names its own lists cannot be counted list by list, and is refused")
    crm_all = _ladder_crm()
    for r in crm_all.recs.values():
        r["reached"] = True
    crm_all.lists_ = [lst("Tax Delinquent", 12), lst("Vacant", 5)] + account_lists()[2:]
    a = sweep.run("ladder_check", crm=crm_all, now=NOW, pol=pol, plan=True)
    check(any("did not narrow any list" in p for p in a.problems) and not any("reach:" in ln for ln in a.lines),
          "a reached preset that narrows nothing is not filtering, and no reach is reported on it")
    none = policy.parse(_with(PLAYBOOK, "ladder_check", reached_preset=""), EMPLOYEES)
    crm_n = _ladder_crm()
    q = sweep.run("ladder_check", crm=crm_n, now=NOW, pol=none, plan=True)
    check(not any("reach:" in ln for ln in q.lines) and not any(x.startswith("count:") for x in crm_n.asked),
          "with no reached preset named, reach is left out and no count is spent on it")

    # bounded, and one thing failing does not hide the rest
    tight = policy.parse(_with(PLAYBOOK, "ladder_check", max_searches=3), EMPLOYEES)
    laddercheck.search = Sold(sold_rows())
    t = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=tight, plan=True)
    check(len(laddercheck.search.bodies) == 3 and any("limit of 3 SiftMap searches is used" in p for p in t.problems)
          and any("Priority 1 rung(s) on the ladder" in ln for ln in t.lines),
          "the run stops searching at its limit, says so, and the feeder check still runs")

    def no_ladder(url):
        if "capture-data" in url:
            raise OSError("timed out")
        return published(url)

    laddercheck.search, laddercheck.fetch_json = Sold(sold_rows()), no_ladder
    f = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=pol, plan=True)
    check(any("could not read the published ladder file for state 99" in p for p in f.problems)
          and any("share of deals by list" in ln for ln in f.lines),
          "a published file that cannot be read is said, and the deals are still read")
    laddercheck.fetch_json = published
    crm_x = _ladder_crm()
    crm_x.broken = ("/api/internal/list/",)
    x = sweep.run("ladder_check", crm=crm_x, now=NOW, pol=pol, plan=True)
    check(any("could not read the account's lists" in p for p in x.problems) and len(laddercheck.search.bodies) == 7
          and any("Priority 1 rung(s) on the ladder" in ln for ln in x.lines),
          "an account whose lists cannot be read has no share reported, and no sold search is spent on it")
    empty = policy.parse(_with(PLAYBOOK, "ladder_check", counties=[]), EMPLOYEES)
    e = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=empty, plan=True)
    check(e.head == "The ladder check has no county to read." and e.problems, "a playbook that names no county is told so")

    # more than one county: each is read on its own, and a county that answers nothing is questioned
    three = policy.parse(_with(PLAYBOOK, "ladder_check", feeders=False, window=False, reached_preset="", counties=[
        dict(ALDER), {"fips": "99003", "name": "Birch", "state": "ZZ"}, {"fips": "99005", "name": "Cedar", "state": "ZZ"}]), EMPLOYEES)
    laddercheck.search = Sold(sold_rows())
    c3 = sweep.run("ladder_check", crm=_ladder_crm(), now=NOW, pol=three, plan=True)
    check(any(ln.startswith("Alder: 208 investor deal(s)") for ln in c3.lines)
          and any(ln.startswith("Birch: 1 investor deal(s) in 6 complete months. 1 (100.0%) were records here") for ln in c3.lines),
          "each county is read on its own months and its own deals: %s" % [ln[:40] for ln in c3.lines])
    check(c3.problems == ["Cedar: no investor purchase was found in 6 months. Check the county's name and state in the playbook "
                          "before reading that as a market with no buyers"],
          "a county with no sale at all in six months is questioned, because a name the search cannot resolve looks the same: %s" % c3.problems)
    check(c3.head.startswith("Ladder check for Alder, Birch and Cedar, April to September 2026: 209 investor deal(s) read"),
          "the headline names every county read: %s" % c3.head)
    check(c3.tally["lists with too few sales to read a rate from"] >= 1 and not any("Birch, this account's own doors per deal" in ln for ln in c3.lines),
          "a list with fewer than five sales has no rate read from it")

    # a stage switched to live on a live box still has nothing to write
    config.MODE = "live"
    try:
        live = policy.parse(_with(PLAYBOOK, "ladder_check", mode="live"), EMPLOYEES)
        laddercheck.search = Sold(sold_rows())
        crm_live = _ladder_crm()
        lv = sweep.run("ladder_check", crm=crm_live, now=NOW, pol=live)
        check(lv.live and crm_live.writes == [] and lv.writes == 0 and all(a.startswith(("/api/internal/", "count:")) for a in crm_live.asked),
              "switched to live on a live box, the ladder check still only reads")
    finally:
        config.MODE = "dry"

    # the standing layered count: named filters from the playbook, counted and kept
    layered = policy.parse(_with(PLAYBOOK, "ladder_check", validate=False, feeders=False, window=False, layers=[
        {"name": "county", "filters": {}}, {"name": "under the cap", "filters": {"value_max": 100000}},
        {"name": "built by 2010", "filters": {"value_max": 100000, "extra_year_built_max": 2010}},
        {"name": "mistyped", "filters": {"value_max": 100000, "extra_year_built_max": 2010, "years_built_max": 1900}}]), EMPLOYEES)
    rows = sold_rows() + [{"id": "c%d" % i, "county": "Alder", "sold": "2020-01-01", "value": 90000, "year": 2000 + i} for i in range(20)]
    laddercheck.search = Sold(rows)
    crm_l = _ladder_crm()
    lay = sweep.run("ladder_check", crm=crm_l, now=NOW, pol=layered, plan=True)
    check(lay.lines == ["Alder, records by layer: county 233, under the cap 20, built by 2010 11, mistyped 11."],
          "each layer the playbook names is counted, in order: %s" % lay.lines)
    check(lay.problems == ["Alder: the layer 'mistyped' counts the same as the one above it (11), so its filter may be ignored"],
          "a layer whose filter changes nothing is called out: an unknown key is answered as if it were not there")
    check(crm_l.asked == [] and json.loads(store.kv_get("autopilot:ladder:2026-11"))["counties"]["Alder"]["layers"]["built by 2010"] == 11,
          "with the other parts switched off only the layers are counted, and they are kept")

    # nothing here can add a record or change a feeder
    code = Path(laddercheck.__file__).read_text(encoding="utf-8")
    check("add-properties" not in code and code.count('method="POST"') == 1 and "MAP + SEARCH_PATH" in code
          and all(v not in code for v in ('"PATCH"', '"PUT"', '"DELETE"')),
          "the only thing the module can post to on SiftMap is the search")
    check(laddercheck.county_address(ALDER)["search"] == "Alder" and laddercheck._body(ALDER, date(2026, 4, 1), date(2026, 4, 30), 250)["result_index"] == 251,
          "the county is searched by its bare name, and a page is asked for by row offset")
    try:
        laddercheck.fetch_json = None
        laddercheck._get_json("https://example.com/capture-data/99.json")
        check(False, "a file from anywhere but the learning site is refused")
    except laddercheck.LadderError:
        check(True, "a file from anywhere but the learning site is refused")
    finally:
        laddercheck.fetch_json = published


def _with(playbook: dict, stage: str, **settings) -> dict:
    p = copy.deepcopy(playbook)
    p["stages"][stage].update(settings)
    return p


# ------------------------------------------------------------------------------------ the conventions check
def _clean_account(**kw):
    recs = []
    for i in range(6):
        r = _rec("m%d" % i, "%d Mail Rd" % (20 + i))
        r["lists"] = ["Vacant"] if i < 4 else ["Foreclosure"]
        if i < 3:
            r["last_direct_mailed"] = "2026-11-03"
            r["tags"] = r["tags"] + ["Mailed 2026-11"]
        recs.append(r)
    base = dict(
        records=recs,
        lists=[lst("Vacant", 4), lst("Foreclosure", 2), lst("Probate", 60), lst("Pre-Probate/Deceased", 9), lst("DNC", 30),
               lst("Dream 100", 100), lst("Dispo - VIP Buyers", 234), lst("Tax Delinquent", 600), lst("Zombie", 0),
               lst("Important Owners", 3), lst("Alder P1-01 - Vacant", 5), lst("Alder P1-02 - Vacant", 5)],
        tags=[tag("FTM"), tag("Courthouse Data"), tag("Priority 1"), tag("Mailed 2026-11", 3), tag("Mailed 2026-10", 9),
              tag("D4D")],
        folders={"05. FTM - CALL": [{"uuid": "p1", "title": "FTM - 02 Ready to Call", "quick_filter": True},
                                    {"uuid": "p2", "title": "FTM - 03 Call Attempt 1", "quick_filter": True}],
                 "06. FTM - MAIL": [{"uuid": "p3", "title": "FTM - Mail 01", "quick_filter": False}]})
    base.update(kw)
    return DataCRM(**base)


def _conventions(check, store, conventions, policy, sweep) -> None:
    print("  conventions check")
    pol = policy.parse(copy.deepcopy(PLAYBOOK), EMPLOYEES)

    def go(crm, **settings):
        p = policy.parse(_with(PLAYBOOK, "conventions_check", **settings), EMPLOYEES) if settings else pol
        return sweep.run("conventions_check", crm=crm, now=NOW, pol=p, plan=True)

    crm = _clean_account()
    s = go(crm)
    check(s.head == "The account follows its conventions (12 lists and 6 tags read)." and s.acted == 0 and not s.lines and not s.problems,
          "an account that follows its conventions is told so in one line: %s %s %s" % (s.head, s.lines, s.problems))
    check(crm.writes == [] and s.writes == 0, "the check changes nothing")
    check(len(crm.asked) <= 9, "a clean check is a handful of reads: %d (%s)" % (len(crm.asked), crm.asked))
    said = sweep.slack_text({"workflow": "conventions_check", "source": "schedule"}, s.as_dict(), now=NOW)
    check(said.startswith(":white_check_mark: *conventions check*") and "follows its conventions" in said,
          "the weekly line is posted even when nothing is wrong, so a quiet week is not mistaken for a check that did not run")
    check(json.loads(store.kv_get("autopilot:conventions:last"))["broken"] == 0, "the result is kept for the weekly QA")

    # lists name the distress
    lists = conventions.check_lists([lst("D4D Top 3000", 3000), lst("Courthouse Import", 40), lst("Important Owners"),
                                     lst("0a1b2c3d-1111-2222-3333-444455556666", 0), lst("Judgments", 8), lst("Judgements", 0),
                                     lst("Alder P1-01 - Vacant"), lst("Alder P1-02 - Vacant"), lst("Probate"),
                                     lst("Pre-Probate/Deceased"), lst("Probate Attorneys", 5), lst("Dispo - Courthouse Buyers"),
                                     lst("DNC"), lst("Vacant")], lambda k, d=None: None)
    joined = "\n".join(lists)
    check("2 list(s) name where the data came from, which is a tag's job: D4D Top 3000, Courthouse Import." in joined,
          "a list named for where the data came from is named: %s" % lists)
    check("Important Owners" not in joined and "Dispo - Courthouse Buyers" not in joined and "DNC" not in joined,
          "a word inside another word is not a match, and the lists kept on purpose are left alone")
    check("1 list(s) are titled with an id instead of a name (0a1b2c3d..., 0 records)." in joined, "a list titled with an id is named")
    check("1 pair(s) of lists are the same name a keystroke apart: Judgements / Judgments." in joined,
          "two lists one letter apart are named")
    check("P1-01" not in joined, "a numbered series is not a typing slip")
    check("Probate is kept in two lists (probate and pre-probate); 1 more carry the word: Probate Attorneys." in joined,
          "a third probate list is named")
    own = conventions.check_lists([lst("Door Knock Leads"), lst("D4D Top 3000")],
                                  lambda k, d=None: {"source_words": ["door knock"], "known_lists": []}.get(k))
    check(len(own) == 1 and "Door Knock Leads" in own[0] and "D4D" not in own[0], "the words that mark a source are the playbook's to set")

    # tags name the source
    tags = conventions.check_tags([tag("Courthouse Data, code_violation, Alder"), tag("Foreclosure"), tag("foreclosure"),
                                   tag("Vacant"), tag("FTM"), tag("D4D")], [lst("Vacant"), lst("Foreclosure"), lst("D4D Top 3000")],
                                  lambda k, d=None: None)
    tj = "\n".join(tags)
    check('1 tag(s) have a comma in the name, so several tags were sent as one string: "Courthouse Data, code_violation, Alder".' in tj,
          "one tag with commas in it is several tags sent as a string")
    check("1 tag(s) exist twice with different capitals: Foreclosure / foreclosure." in tj, "two tags that differ only by capitals are named")
    check("2 distress name(s) are kept as a tag and as a list at once: Foreclosure, Vacant." in tj and "FTM" not in tj and "D4D" not in tj,
          "a distress kept as a tag and as a list is named; a source tag is what a tag is for")
    allowed = conventions.check_tags([tag("Vacant")], [lst("Vacant")], lambda k, d=None: {"allow_tags": ["vacant"]}.get(k))
    check(allowed == [], "a tag the playbook allows is left alone")

    # the zombie set
    z = go(_clean_account(lists=[lst("Vacant", 4), lst("Foreclosure", 2), lst("Probate", 60)]))
    check(z.lines == ["No zombie set: 0 record(s) are on both Vacant and Foreclosure, and no list or tag names them."],
          "an account with no zombie set is told so, with the count: %s" % z.lines)
    both = _clean_account(lists=[lst("Vacant", 5), lst("Foreclosure", 2), lst("Probate", 60)])
    both.recs["m5"]["lists"] = ["Foreclosure", "Vacant"]
    zb = go(both)
    check(zb.lines == ["No zombie set: 1 record(s) are on both Vacant and Foreclosure, and no list or tag names them."],
          "with no zombie set, the records on both lists are counted from the one list filter that is proven: %s" % zb.lines)
    check(sum(1 for a in both.asked if a == "count:any_lists") == 1, "the two list sizes come from the listing, so the overlap costs one count")
    ztag = go(_clean_account(lists=[lst("Vacant", 4), lst("Foreclosure", 2), lst("Probate", 60)], tags=[tag("Zombie"), tag("Mailed 2026-11", 3)]))
    check(not any("zombie" in ln.lower() for ln in ztag.lines), "a tag that names the set counts as the set")
    zno = go(_clean_account(lists=[lst("Vacant", 4), lst("Probate", 60)]))
    check(zno.lines == ["No zombie set (vacant and in foreclosure), and the account has no list titled Foreclosure to build one from."],
          "with one of the two lists missing, the set cannot be counted and that is said: %s" % zno.lines)

    # the inbound source field
    nof = go(_clean_account(fields={}))
    check(nof.lines == ["No custom field named Inbound Source: an inbound lead has nowhere to say how it found us."],
          "a missing inbound source field is named: %s" % nof.lines)

    # month tags on mail
    untagged = _clean_account()
    untagged.recs["m4"]["last_direct_mailed"] = "2026-11-05"
    untagged.recs["m5"]["last_direct_mailed"] = "2026-10-20"
    mt = go(untagged)
    check(mt.lines == ["2 of 5 record(s) mailed since 2026-10-07 carry no month tag."], "mailed records with no month tag are counted: %s" % mt.lines)
    notag = go(_clean_account(tags=[tag("FTM"), tag("Mailed Nov", 3)]))
    check(notag.lines[0] == "1 mail tag(s) are not in the shape Mailed 2026-11: Mailed Nov."
          and notag.lines[1] == "3 record(s) were mailed since 2026-10-07 and no month tag exists for it (Mailed 2026-11).",
          "a mail tag in another shape is named, and mail with no month tag at all is said: %s" % notag.lines)
    quiet = _clean_account()
    for r in quiet.recs.values():
        r.pop("last_direct_mailed", None)
    check(go(quiet).lines == [], "an account that mailed nothing lately has no mail tag to miss")

    # the callers' presets are starred
    plain = _clean_account(folders={"05. FTM - CALL": [{"uuid": "p1", "title": "FTM - 02 Ready to Call", "quick_filter": False},
                                                       {"uuid": "p2", "title": "FTM - 03 Call Attempt 1", "quick_filter": True}]})
    st = go(plain)
    check(st.lines == ["1 of the callers' presets are not starred: FTM - 02 Ready to Call."],
          "a caller preset that is not starred is named, and only the callers' folder is read: %s" % st.lines)
    loose = go(plain, caller_folders=["05 ftm - call"])
    check(loose.lines == st.lines, "a folder is found by its letters and digits, whatever its dots and capitals")
    gone = go(_clean_account(), caller_folders=["05. FTM - CALL", "20. CALLER QUEUES"])
    check(gone.lines == ["No preset folder titled 20. CALLER QUEUES, so its presets could not be checked."],
          "a caller folder the account does not have is said: %s" % gone.lines)
    blind = go(_clean_account(folders={"05. FTM - CALL": [{"uuid": "p1", "title": "FTM - 02 Ready to Call"}]}))
    check(blind.lines == [] and blind.problems == ["The preset listing did not say whether 1 preset(s) are starred, so those were not checked."]
          and "1 check(s) could not be made" in blind.head,
          "a listing that does not say what is starred is reported as unchecked, never as a wall of unstarred presets")
    unnamed = go(_clean_account(), caller_folders=[])
    check(unnamed.problems == ["No caller folder is named in the playbook, so starred presets were not checked."],
          "with no caller folder named, the starred check says it did not run: %s" % unnamed.problems)

    # account settings
    off = go(_clean_account(user={"auto_update": False}))
    check(off.lines == ["Auto update is off in Settings, so records and owners are not being refreshed."],
          "auto update switched off is named: %s" % off.lines)
    silent = go(_clean_account(user={}))
    check(silent.lines == ["The account did not say whether auto update is on, so it was not checked."],
          "a setting the account does not report is said as unchecked, never as on: %s" % silent.lines)

    # everything at once, in one run, with one check failing
    messy = _clean_account(
        lists=[lst("Vacant", 4), lst("Foreclosure", 2), lst("D4D Top 3000", 3000), lst("Probate", 60)],
        tags=[tag("FTM"), tag("Vacant"), tag("Mailed 2026-11", 3)], fields={}, user={"auto_update": False})
    messy.broken = ("/api/internal/filter-preset-folder/",)
    m = go(messy)
    check(m.acted == 5 and len(m.lines) == 5 and m.head == "5 thing(s) break the account's conventions (4 lists and 3 tags read). Nothing was changed.",
          "every broken convention is listed in one run: %s" % m.lines)
    check(m.problems == ["the starred presets check could not be made (HTTP 500 on GET /api/internal/filter-preset-folder/?type=properties&limit=999)"],
          "a check that could not be made is said, and it does not hide the others: %s" % m.problems)
    check(messy.writes == [] and dict(m.tally)["conventions broken: lists"] == 1, "and still nothing is changed")
    skipped = go(_clean_account(fields={}), skip=["inbound"])
    check(skipped.lines == [], "a check the playbook skips is not run")
    down = _clean_account()
    down.broken = ("/api/internal/tag/",)
    d = go(down)
    check(d.head == "The conventions check could not read the account." and d.problems, "an account that cannot be read is said, not passed")

    config.MODE = "live"
    try:
        live = policy.parse(_with(PLAYBOOK, "conventions_check", mode="live"), EMPLOYEES)
        crm_live = _clean_account(fields={}, user={"auto_update": False})
        lv = sweep.run("conventions_check", crm=crm_live, now=NOW, pol=live)
        check(lv.live and lv.acted == 2 and crm_live.writes == [] and lv.writes == 0,
              "switched to live on a live box, the conventions check still fixes nothing: it lists and stops")
    finally:
        config.MODE = "dry"

    check(conventions.one_letter_apart("judgments", "judgements") and conventions.one_letter_apart("taxsale", "taxsales")
          and not conventions.one_letter_apart("alderp101", "alderp102") and not conventions.one_letter_apart("vacant", "vacant")
          and not conventions.one_letter_apart("liens", "lien") and not conventions.one_letter_apart("probate", "eviction"),
          "one keystroke apart means one letter, on names long enough to mean it")
    code = Path(conventions.__file__).read_text(encoding="utf-8")
    check("ctx.write" not in code and all(v not in code for v in ('"POST"', '"PATCH"', '"PUT"', '"DELETE"')),
          "the conventions module has no way to write")
    for name in ("laddercheck.py", "conventions.py"):
        text = (Path(conventions.__file__).parent / name).read_text(encoding="utf-8")
        check(chr(8212) not in text and chr(8211) not in text and chr(8) not in text, "no long dash or backspace byte in %s" % name)
    here = Path(__file__).read_text(encoding="utf-8")
    check(chr(8212) not in here and chr(8211) not in here and chr(8) not in here, "no long dash or backspace byte in this selftest")
