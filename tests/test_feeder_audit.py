"""Feeder audit: what each kind of SiftMap feeder gets proposed, and that the tool can only read.
Zero network: invented feeders in two invented counties."""
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.autopilot import feeder_audit as fa  # noqa: E402

BOX = {"value_max": 700000, "value_min": 100000, "extra_mls_status": ["not_listed"], "type_single_family": True}


def row(i, name, filters, tags=(), lists=(), auto=True, county="Alder", active=True, scope="account"):
    return {"id": i, "name": name, "description": None, "is_active": active, "scope": scope,
            "account_uuid": None if scope == "default" else "acct", "auto_add_enabled": auto,
            "replace_owners_enabled": False, "email_enabled": False, "tags": list(tags) or None,
            "lists": list(lists) or None, "limit": None, "limit_type": None, "is_favorite": False,
            "created_by": {"email": "someone@example.com"}, "updated": "2026-10-01T10:00:00Z",
            "filter_data": {"filters": dict(filters), "addresses": [{"county": county, "searchType": "county"}]}}


def listing():
    free_clear = {"value_max": 700000, "value_min": 100000, "value_type": "est_value", "distressors_method": "custom",
                  "type_single_family": True, "extra_equity_percent_min": 100}
    return [
        row(1, "Absentee Owners", {"preset_absentee_owners": True}, auto=False, scope="default"),
        row(10, "Alder - Tier 2 - Vacant", dict(BOX, distressors_include="is_vacant_property",
                                               distressors_include_min_match=1), tags=("Tier 2", "Vacant")),
        row(11, "Birch - Tier 2 - Probate", dict(BOX, distressors_include="is_probate_properties",
                                                distressors_include_min_match=1), tags=("Tier 2", "Probate"),
            county="Birch"),
        row(12, "Alder - Tier 2 - Old One", dict(BOX, distressors_include="is_low_income"), tags=("Tier 2",), auto=False),
        row(20, "Alder - Priority 1 - AI 90+", dict(BOX, investor_off_market_score_min=90), tags=("Tier 2", "Priority 1")),
        row(21, "Alder - Priority 2 - Absentee + Tax Delinquent",
            dict(BOX, distressors_include="is_recent_deliquent", preset_absentee_owners=True,
                 distressors_include_min_match=1), tags=("Tier 2", "Priority 2")),
        row(22, "Birch - Priority 2 - AI 70+", dict(BOX, investor_off_market_score_min=70), tags=("Priority 2",),
            county="Birch"),
        row(30, "Alder-Priority 1- Free & Clear + Senior + Vacant",
            dict(free_clear, distressors_include="is_senior_homeowners,is_vacant_property"), tags=("Priority 1",)),
        row(31, "Priority 1- Free & Clear + Senior + Vacant",
            dict(free_clear, distressors_include="is_senior_homeowners,is_vacant_property",
                 extra_mls_status=["not_listed"]), tags=("Priority 1",)),
        row(32, "Prioity 1-Free & Clear + Senior + Vacant",
            dict(free_clear, distressors_include="is_vacant_property,is_senior_homeowners"), tags=("Priority 1",)),
        row(40, "Obituary + Alder County", {"distressors_method": "custom", "type_single_family": True,
                                           "distressors_include": "is_obituary_property",
                                           "distressors_include_min_match": 1}),
        row(41, "Alder - Obituary", {"extra_mls_status": ["not_listed"], "type_single_family": True,
                                    "distressors_include": "is_pre_probate", "distressors_include_min_match": 1},
            tags=("Obituary",), auto=False),
        row(50, "Dispo - Pending Flips - Alder", {"type_single_family": True,
                                                 "extra_last_sale_investor_transaction_type": ["pending"]},
            tags=("Dispo Buyer",), lists=("Dispo - Flip Buyers",)),
        row(3241, "Recently Sold - Suppress", {"in_my_account_mode": "in", "extra_last_sale_date_min": "2023-01-01"},
            tags=("recently sold",), auto=False),
        row(4076, "Alder - Recently Sold", {"in_my_account_mode": "in", "type_single_family": True,
                                           "extra_last_sale_date_min": "2023-01-01"}, tags=("recently sold",), auto=False),
        row(60, "Alder - Old Experiment", dict(BOX), auto=True, active=False),
    ]


def plan_of(rows=None):
    return fa.build_plan(rows if rows is not None else listing(), day="2026-11-13", pages=2, read_at="2026-11-12T23:00:00")


def change(plan, i):
    return next(c for c in plan["changes"] if c["id"] == i)


def test_each_feeder_is_read_into_plain_facts():
    f = fa.facts(listing()[5])
    assert (f["id"], f["title"], f["counties"]) == (21, "Alder - Priority 2 - Absentee + Tax Delinquent", ["Alder"])
    assert f["auto_add"] and f["active"] and f["tags"] == ["Tier 2", "Priority 2"] and f["lists"] == []
    assert (f["value_min"], f["value_max"], f["value_type"], f["equity_min"]) == (100000, 700000, None, None)
    assert f["listing"] == ["not_listed"] and f["distress"] == ["is_recent_deliquent"] and f["distress_min"] == 1
    assert f["other"] == {"preset_absentee_owners": True}
    assert "created_by" not in f and "email" not in json.dumps(f)        # who made it stays in the account
    assert fa.facts(listing()[0])["account_filter"] is False             # a built-in SiftMap filter
    kinds = {r["id"]: fa.kind_of(fa.facts(r)) for r in listing()}
    assert kinds[10] == kinds[11] == kinds[12] == "retired"
    assert kinds[20] == kinds[21] == kinds[22] == kinds[30] == kinds[32] == "priority"
    assert kinds[40] == kinds[41] == "obituary" and kinds[50] == "other"
    assert kinds[3241] == kinds[4076] == "sold_tagging"
    assert kinds[11] == "retired"                                        # a retired probate feeder is not an obituary one


def test_retired_feeders_that_still_add_are_switched_off():
    plan = plan_of()
    off = [c for c in plan["changes"] if c["group"] == "retired"]
    assert [c["id"] for c in off] == [10, 11]
    assert all(c["set"] == {"auto_add_enabled": False} and "retired" in c["why"] for c in off)
    assert not [c for c in plan["changes"] if c["id"] == 12]             # already off: nothing to do


def test_the_retired_tag_comes_off_the_priority_feeders():
    plan = plan_of()
    tag = [c for c in plan["changes"] if c["group"] == "tag"]
    assert [c["id"] for c in tag] == [20, 21]
    assert change(plan, 20)["set"] == {"tags": ["Priority 1"]} and change(plan, 21)["set"] == {"tags": ["Priority 2"]}
    assert not [c for c in plan["changes"] if c["id"] == 22]             # never carried it
    # the retired feeders keep their own tag: they are switched off, not re-tagged
    assert all(c["group"] == "retired" for c in plan["changes"] if c["id"] in (10, 11))


def test_near_copies_become_one_and_the_one_kept_leaves_out_listed_houses():
    plan = plan_of()
    copies = [c for c in plan["changes"] if c["group"] == "copies"]
    assert sorted(c["id"] for c in copies) == [30, 32]
    assert all(c["keep_id"] == 31 and c["set"] == {"auto_add_enabled": False} for c in copies)
    assert "leaves out listed houses" in change(plan, 30)["why"]
    # the same distress flags in another order are the same feeder; another county is not
    a, b = fa.facts(listing()[7]), fa.facts(listing()[9])
    assert fa.signature(a) == fa.signature(b)
    assert fa.signature(a) != fa.signature(dict(a, counties=["Birch"]))
    # a retired twin of a Priority feeder is switched off as retired, never reported as a copy too
    rows = listing() + [row(13, "Alder - Tier 2 - Absentee + Tax Delinquent",
                            dict(BOX, distressors_include="is_recent_deliquent", preset_absentee_owners=True,
                                 distressors_include_min_match=1), tags=("Tier 2",))]
    groups = [c["group"] for c in plan_of(rows)["changes"] if c["id"] in (13, 21)]
    assert sorted(groups) == ["retired", "tag"]


def test_obituary_feeders_gain_the_buy_box_and_a_tag():
    plan = plan_of()
    c = change(plan, 40)
    assert c["set"]["filters"] == {"value_max": 700000, "value_type": "est_value", "extra_equity_percent_min": 30,
                                   "extra_mls_status": ["not_listed"]}
    assert c["set"]["tags"] == ["Obituary"]
    for word in ("value cap of $700,000", "equity floor of 30%", "listed houses", "tag Obituary"):
        assert word in c["do"], word
    # one that already leaves out listed houses and carries the tag gains only what it lacks
    d = change(plan, 41)
    assert d["set"] == {"filters": {"value_max": 700000, "value_type": "est_value", "extra_equity_percent_min": 30}}
    assert "Auto-add is off" in d["why"] and "tag" not in d["do"]
    # a looser cap or floor is tightened, a tighter one is kept
    rows = [row(42, "Birch Obituary Data", {"distressors_include": "is_obituary_property", "value_max": 900000,
                                           "value_type": "est_value", "extra_equity_percent_min": 50,
                                           "extra_mls_status": ["not_listed"]}, tags=("obituary",), county="Birch")]
    e = change(plan_of(rows), 42)
    assert e["set"] == {"filters": {"value_max": 700000}} and "today: $900,000" in e["do"]


def test_the_sold_tagging_is_off_on_purpose_and_never_proposed():
    plan = plan_of()
    assert [n["id"] for n in plan["never_on"]] == [3241, 4076]
    assert plan["never_on_missing"] == [4084]
    assert not [c for c in plan["changes"] if c["id"] in fa.NEVER_ON]
    plans = {f["id"]: f["plan"] for f in plan["feeders"]}
    assert "Never switch back on" in plans[3241] and "Never switch back on" in plans[4076]
    assert not [o for o in plan["observations"] if o.startswith("ALARM")]
    # one found switched on is the loudest line in the report, and still nothing is proposed for it
    rows = listing()
    rows[14]["auto_add_enabled"] = True
    loud = plan_of(rows)
    assert any(o.startswith("ALARM") and "[4076]" in o for o in loud["observations"])
    assert not [c for c in loud["changes"] if c["id"] == 4076]
    assert "AUTO-ADD IS ON: THIS IS WRONG" in fa.render_md(loud)
    # any other filter that hands out the sold tag is treated the same way
    other = plan_of([row(9000, "Cedar - Sold Sweep", {"in_my_account_mode": "in"}, tags=("Recently Sold",), auto=False)])
    assert [n["id"] for n in other["never_on"]] == [9000] and not other["changes"]


def test_the_summary_adds_up_and_a_second_audit_proposes_nothing():
    rows = listing()
    plan = plan_of(rows)
    s = plan["summary"]
    assert (s["filters_listed"], s["built_in_skipped"], s["feeders"], s["active"], s["deleted"]) == (16, 1, 15, 14, 1)
    assert (s["auto_add_on"], s["auto_add_off"]) == (10, 4)
    assert s["by_group"] == {"retired": 2, "tag": 2, "copies": 2, "obituary": 2} and s["changes"] == 8
    assert s["auto_add_on_after"] == 6
    # apply every proposal to the listing by hand, audit again: nothing left to propose
    by_id = {r["id"]: r for r in rows}
    for c in plan["changes"]:
        r = by_id[c["id"]]
        if "auto_add_enabled" in c["set"]:
            r["auto_add_enabled"] = c["set"]["auto_add_enabled"]
        if "tags" in c["set"]:
            r["tags"] = c["set"]["tags"]
        r["filter_data"]["filters"].update(c["set"].get("filters") or {})
    again = plan_of(rows)
    assert again["changes"] == [], again["changes"]
    assert again["summary"]["auto_add_on"] == 6


def test_things_worth_a_look_are_said():
    notes = "\n".join(plan_of()["observations"])
    assert "Deleted but still marked auto-add: [60] Alder - Old Experiment." in notes
    assert "Auto-adding feeders [21]: each uses the absentee preset switch" in notes
    assert "nothing said on how many must match" in notes and "[31]" in notes
    assert "No value cap at all on these auto-adding feeders: [50] Dispo - Pending Flips - Alder." in notes
    assert "does not say which value it means" in notes and "on 5 auto-adding feeders" in notes
    assert "No tag and no list" in notes and "[40]" in notes


def test_the_listing_is_followed_to_its_end():
    rows = listing()
    pages = {fa.MAP + "/filters/?page_size=100": {"count": 16, "next": fa.MAP + "/filters/?offset=10&page_size=100",
                                                 "results": rows[:10]},
             fa.MAP + "/filters/?offset=10&page_size=100": {"count": 16, "next": None, "results": rows[10:]}}
    asked, naps = [], []
    got = fa.fetch_filters("key", get=lambda url, key: asked.append(url) or pages[url], sleep=naps.append, gap=2.0)
    assert len(got["rows"]) == 16 and got["pages"] == 2 and asked == list(pages)
    assert naps == [2.0]                                                 # it waits between pages, not before the first
    # a listing that stops short, repeats itself or never ends is an error, never a quiet short answer
    short = {fa.MAP + "/filters/?page_size=100": {"count": 16, "next": None, "results": rows[:10]}}
    for bad, word in ((short, "says 16"),
                      ({fa.MAP + "/filters/?page_size=100": {"count": 20, "next": fa.MAP + "/filters/?page_size=100",
                                                           "results": rows[:10]}}, "did not end"),
                      ({fa.MAP + "/filters/?page_size=100": {"results": "nope"}}, "unexpected")):
        try:
            fa.fetch_filters("key", get=lambda url, key, bad=bad: bad[url], sleep=lambda s: None, max_pages=3)
            raise AssertionError("a broken listing was accepted")
        except fa.FeederError as e:
            assert word in str(e), e
    twice = {fa.MAP + "/filters/?page_size=100": {"next": fa.MAP + "/filters/?offset=10", "results": rows[:10]},
             fa.MAP + "/filters/?offset=10": {"next": None, "results": rows[5:]}}
    try:
        fa.fetch_filters("key", get=lambda url, key: twice[url], sleep=lambda s: None)
        raise AssertionError("a repeated filter was accepted")
    except fa.FeederError as e:
        assert "repeated" in str(e)


def test_the_tool_can_only_read_and_only_siftmap():
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"results": [], "next": null, "count": 0}'

    real = fa.urllib.request.urlopen
    fa.urllib.request.urlopen = lambda req, timeout=0: seen.append(req) or Answer()
    try:
        assert fa.http_get(fa.MAP + "/filters/", "the-key") == {"results": [], "next": None, "count": 0}
        req = seen[0]
        assert req.get_method() == "GET" and req.data is None
        assert req.get_header("Authorization") == "Api-Key the-key"
        # a `next` that points anywhere else never gets the key
        for url in ("https://example.com/filters/", "https://map.reisift.io.example.com/filters/", "http://map.reisift.io/x"):
            try:
                fa.http_get(url, "the-key")
                raise AssertionError("the key was sent to %s" % url)
            except fa.FeederError:
                pass
        assert len(seen) == 1
        # a refused key stops the run: one request, no second credential
        def refuse(req, timeout=0):
            seen.append(req)
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail": "no"}'))
        fa.urllib.request.urlopen = refuse
        try:
            fa.http_get(fa.MAP + "/filters/", "the-key")
            raise AssertionError("a 401 was swallowed")
        except fa.FeederAuthError as stop:
            assert "does not look for another credential" in str(stop)
        assert len(seen) == 2
    finally:
        fa.urllib.request.urlopen = real
    # nothing in the module can send a body: no write verb appears outside its own description
    code = (ROOT / "src/agents/autopilot/feeder_audit.py").read_text(encoding="utf-8").split('"""', 2)[2]
    for verb in ('"POST"', '"PATCH"', '"PUT"', '"DELETE"', "data="):
        assert verb not in code, verb
    assert code.count('method="GET"') == 1


def test_the_report_is_for_the_owner():
    plan = plan_of()
    md = fa.render_md(plan)
    assert md.startswith("# SiftMap feeders") and "Nothing has been changed" in md
    for want in ("## Off on purpose: never switch back on", "[3241] Recently Sold - Suppress: auto-add is OFF",
                 "[4084] was not in the listing", "### 1. Switch auto-add off", "[10] Alder - Tier 2 - Vacant",
                 "### 2. Take the Tier 2 tag off", "### 3. Near-copies become one", "### 4. Obituary feeders",
                 "## Every feeder", "$100,000 to $700,000 (type not set)", "$100,000 to $700,000 (estimated)",
                 "Off on purpose since 2026-11-12. Never switch back on.", "feeders adding records daily: 6 instead of 10",
                 "Auto-add off on 2 retired Tier 2 feeders, the Tier 2 tag off 2 Priority feeders, 2 near-copies",
                 "last sale investor transaction type: pending", "preset absentee owners: yes",
                 "Nothing here was sized"):
        assert want in md, want
    assert md.count("\n| ") == 1 + 15                                    # the header and one row per feeder
    # one row per feeder first, then the changes, as the owner reads it
    assert md.index("## Off on purpose") < md.index("## Every feeder") < md.index("## Proposed changes") \
        < md.index("### 1. Switch auto-add off") < md.index("## Worth a look") < md.index("## What this audit did not do")
    for bad in ("someone@example.com", "created_by", "filter_data", "auto_add_enabled", chr(8212), chr(8211)):
        assert bad not in md, bad
    with tempfile.TemporaryDirectory() as tmp:
        j, m = fa.write_plan(plan, tmp)
        assert j.name == "plan_2026-11-13.json" and m.name == "plan_2026-11-13.md"
        saved = json.loads(j.read_text(encoding="utf-8"))
        assert saved["summary"] == plan["summary"] and "someone@example.com" not in j.read_text(encoding="utf-8")
    # an account with nothing to fix says so in every section
    quiet = fa.render_md(plan_of([row(70, "Alder - Priority 1 - AI 90+", dict(BOX), tags=("Priority 1",))]))
    assert quiet.count("Nothing to do: no feeder needs this today.") == 4


def test_the_module_holds_no_long_dash_or_backspace_byte():
    for name in ("src/agents/autopilot/feeder_audit.py", "tests/test_feeder_audit.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert chr(8212) not in text and chr(8211) not in text and chr(8) not in text, name


def main() -> int:
    names = [n for n in list(globals()) if n.startswith("test_")]
    for n in names:
        globals()[n]()
        print("ok  " + n)
    print("%d tests passed" % len(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
