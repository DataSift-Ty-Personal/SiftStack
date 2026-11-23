"""Always-in selftest: the Top 1,000's always_include (the owner's ruling, 2026-11-17: condemned, code
violation and eviction records are in the Top 1,000 "no matter what").

What is checked: an owner with a house on an always-in list or tag takes a slot before the ranking
fills the rest, whatever its rank, its discount room, the buy box or the people-only rule say; the
hard holds still keep a house out (sold, a live lead, first to market, Do Not Market, here or on the
owner's other house), and mail only does not; the owner's other houses come in on the ranking's own
rules; a holder that is always in is never taken off by the keep_in_round pass, live included; a
title the account does not hold is said; a pool kept under other titles is read again; the playbook
check refuses a malformed block; and with no always_include the run is what it was.

Zero network: the deal-odds selftest's fake account (owner names on the search rows), fake SiftMap
and fake model. Every name, street, number and id here is invented.
"""
from __future__ import annotations

import copy
import csv
import json
import os
from pathlib import Path

from . import config
from .selftest_autopilot import EMPLOYEES, PRESETS
from .selftest_deal_odds import MODEL, BandCRM, OddsMap
from .selftest_textlane import mob
from .selftest_tracks import NOV, TRACK_PRESETS, _wipe, house, playbook

ALWAYS = {"lists": ["Condemned", "Eviction"], "tags": ["code_violation"]}


def account() -> list:
    """R1 to R3 rank on their odds. C, E, V and M are always in: C holds the tag already with too little
    room for a fee and has a plain second house, E is a company, V sits over the buy box and outside
    the sweep, M is mail only. S (sold), D (Do Not Market), F (first to market), L (a live lead) and G
    (Do Not Market on its other house) are always-in records the hard holds keep out."""
    out = [
        house("r1", "1 Always St", "o-r1", value=150000, equity=100.0, flags=["is_vacant_property"], last_sold="1998-05-01",
              year=1955, phones=[mob(7101, "Dial First")]),
        house("r2", "2 Always St", "o-r2", value=200000, equity=40.0, last_sold="2012-06-01", year=1990),
        house("r3", "3 Always St", "o-r3", value=200000, equity=40.0, last_sold="2012-06-01", year=1991),
        house("c1", "4 Always St", "o-c", value=50000, equity=10.0, lists=["Condemned"], tags=["Top 1000"], last_sold="2015-01-01"),
        house("c2", "5 Always St", "o-c", value=120000, equity=30.0, last_sold="2010-01-01", year=1960),
        house("e1", "6 Always St", "o-e", lists=["Eviction"], first="", last="", company="Example Rentals LLC"),
        house("v1", "7 Always St", "o-v", value=900000, equity=50.0, tags=["code_violation"]),
        house("m1", "8 Always St", "o-m", lists=["Condemned"], tags=["Mail Only"]),
        house("s1", "9 Always St", "o-s", lists=["Condemned"], tags=["recently sold", "Top 1000"]),
        house("d1", "10 Always St", "o-d", lists=["Eviction"], tags=["Do Not Market"]),
        house("f1", "11 Always St", "o-f", lists=["Condemned"], tags=["FTM"]),
        house("l1", "12 Always St", "o-l", lists=["Eviction"], status="Warm Lead"),
        house("g1", "13 Always St", "o-g", lists=["Eviction"]),
        house("g2", "14 Always St", "o-g", tags=["Do Not Market"]),
    ]
    # Out of the agency queue (its fake preset reads the Pool tag): C's condemned house is a holder
    # now, V and S come in only through the always-in titles and the holder tag.
    for r in out:
        if r["uuid"] in ("c1", "v1", "s1"):
            r["tags"] = [t for t in r["tags"] if t != "Pool"]
    return out


def run(check, tmp: Path) -> None:
    from . import store
    from .autopilot import top1000
    saved = (config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD)
    config.DAILY_CAP_USD, config.RUN_CAP_USD = 100000.0, 5.0
    config.DATA_DIR = Path(tmp) / "always_in"
    config.MODE = "dry"
    store.init()
    saved_presets = dict(PRESETS)
    PRESETS.update(TRACK_PRESETS)
    saved_search = top1000.search
    saved_env = os.environ.pop(top1000.ALLOW_ENV, None)
    try:
        _wipe()
        _checks(check, Path(tmp))
    finally:
        PRESETS.clear()
        PRESETS.update(saved_presets)
        top1000.search = saved_search
        if saved_env is not None:
            os.environ[top1000.ALLOW_ENV] = saved_env
        _wipe()
        config.DATA_DIR, config.DAILY_CAP_USD, config.RUN_CAP_USD = saved
        config.MODE = "dry"


def _book(tmp: Path, **over) -> dict:
    path = tmp / "always_in_model.json"
    path.write_text(json.dumps(MODEL), encoding="utf-8")
    return playbook(tmp, score="deal_odds", odds_model=str(path), size=5, individuals_only=True, **over)


def _fresh(sub: str) -> None:
    """A data folder of its own: a settled month on disk would short-circuit the next run."""
    _wipe()
    config.DATA_DIR = Path(config.DATA_DIR).parent / sub


def _checks(check, tmp: Path) -> None:
    from .autopilot import agencyexport, ledger, policy, sweep, top1000, undo
    print("  the Top 1,000's always-in records")
    st0 = policy.parse(_book(tmp), EMPLOYEES).stage("t_top")
    check(top1000.options(st0)["always_include"] == {} and top1000.always_include(top1000.options(st0)) == {"lists": [], "tags": []}
          and top1000.always_key(top1000.always_include({})) == "",
          "always_include is empty unless the block names it")

    # --- without always_include: the run is what it was, and the hand-tagged condemned house loses its tag
    _fresh("always_off")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    r = sweep.run("t_top", crm=crm, now=NOV, pol=policy.parse(_book(tmp), EMPLOYEES), plan=True)
    off = top1000.month_state("t_top", "2026-11")
    check("c1" not in (off.get("tagged") or []) and "e1" not in (off.get("tagged") or [])
          and "v1" not in (off.get("tagged") or []) and not off.get("always"),
          "with no always_include nothing changes: the condemned house with no room for a fee would lose the tag it "
          "was given by hand, and the company and the house over the buy box never get it (the 2026-11-17 gap)")
    check(not any(k.startswith("left out: always in") for k in r.tally) and not any("whatever their rank" in ln for ln in r.lines),
          "and no run line or count speaks of always-in records")

    # --- with always_include: the always-in owners take their slots first
    _fresh("always_on")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    pol = policy.parse(_book(tmp, always_include=copy.deepcopy(ALWAYS)), EMPLOYEES)
    r = sweep.run("t_top", crm=crm, now=NOV, pol=pol, plan=True)
    state = top1000.month_state("t_top", "2026-11")
    check(state.get("always") == ["o-c", "o-e", "o-m", "o-v"],
          "the condemned, eviction and code-violation owners are in: %s" % state.get("always"))
    check(state.get("owners") == ["o-c", "o-e", "o-m", "o-r1", "o-v"],
          "they take their slots first and the ranking fills the rest: one slot of five, for the owner with the best odds")
    tagged = set(state.get("tagged") or [])
    check({"c1", "c2", "e1", "v1", "m1", "r1"} == tagged,
          "every house of an always-in owner gets the tag: the room floor, the buy box and the people rule do not apply "
          "to the always-in house, and the owner's plain second house comes with it: %s" % sorted(tagged))
    check(not ({"s1", "d1", "f1", "l1", "g1", "g2", "r2", "r3"} & tagged),
          "the hard holds still keep a house out, and the ranking's own losers stay out")
    t = r.tally
    check(t.get("left out: always in, but held out: the house sold") == 1
          and t.get("left out: always in, but the owner is held out whole: tagged Do Not Market") == 1
          and t.get("left out: always in, but the owner is held out whole: already in the first-to-market lane") == 1
          and t.get("left out: always in, but the owner is held out whole: a live lead") == 1
          and t.get("left out: always in, but the owner is held out whole: tagged Do Not Market on another house") == 1,
          "each hold is counted under its own reason: a sale holds the house; Do Not Market, first to market and a lead, "
          "here or on the owner's other house, hold the owner: %s" % {k: v for k, v in t.items() if "always in" in k})
    check("r1" in tagged and "m1" in tagged, "mail only closes one channel, so it does not keep an always-in house out")
    check(any(ln.startswith("4 owner(s) are in whatever their rank (always_include: 2 on the Condemned list, "
                            "1 on the Eviction list, 1 tagged code_violation); the ranking filled the other 1 slot(s).")
              for ln in r.lines)
          and any(ln.startswith("5 always-in house(s) were held out anyway") for ln in r.lines),
          "the run says who is in whatever their rank, and how many were held out anyway: %s" % r.lines)
    check(r.head.startswith("Would put the Top 1000 tag on 5 owners (6 houses)") and "5 house(s) would get it and 1 would lose it" in r.head
          and not crm.writes, "a plan says what would change and writes nothing: %s" % r.head)
    rows = list(csv.reader(Path(state["summary"]["file"]).open(encoding="utf-8")))
    decisions = {row[-1]: row[-2] for row in rows[1:]}
    check(decisions.get("c1", decisions.get("c2", "")).startswith("keep: always in, on the Condemned list")
          and decisions.get("e1") == "add: always in, on the Eviction list" and decisions.get("v1") == "add: always in, tagged code_violation"
          and decisions.get("s1") == "remove" and decisions.get("r1") == "add",
          "the ranking file says which owners are always in and why, and the sold holder is the one that loses the tag: %s" % decisions)
    check(not any(line.startswith("rank 1: 4 Always St") for line in r.lines) and any(line.startswith("rank 1: 1 Always St") for line in r.lines),
          "the rank lines describe the ranking, not the owners who are in whatever their rank")
    wait, promoted, _ = agencyexport.top1000_owners(type("C", (), {"policy": pol})(), {"after_stage": "t_top"}, "plan", "2026-11")
    check(not wait and {"o-c", "o-e", "o-m", "o-v"} <= promoted,
          "the agency file keeps the always-in owners out, as this month's Top 1,000")

    # --- live: the always-in holder is never taken off, and every change can be put back
    print("  the always-in records written")
    _fresh("always_live")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    config.MODE = "live"
    try:
        live = policy.parse(_book(tmp, always_include=copy.deepcopy(ALWAYS), top_mode="live"), EMPLOYEES)
        w = sweep.run("t_top", crm=crm, now=NOV, pol=live)
    finally:
        config.MODE = "dry"
    adds = sorted(x[1] for x in crm.writes if x[0] == "add_tags")
    gone = sorted(x[1] for x in crm.writes if x[0] == "remove_tags")
    check(adds == ["c2", "e1", "m1", "r1", "v1"] and gone == ["s1"] and "Top 1000" in crm.recs["c1"]["tags"],
          "live, the always-in houses get the tag, the sold holder loses it, and the condemned holder keeps it: "
          "the keep_in_round pass never takes an always-in owner off (%s / %s)" % (adds, gone))
    changes = ledger.changes()
    check(len(changes) == 6 and len({c["run_id"] for c in changes}) == 1
          and sorted(s["do"] for s in undo.plan(changes[0]["run_id"])) == ["add_tags"] + ["remove_tags"] * 5
          and "got it and 1 lost it" in w.head,
          "every tag change is recorded with what it replaced, so undo puts the month back: %s" % w.head)

    # --- the pool: a pool kept under other titles is read again; a title the account lacks is said
    print("  the always-in pool")
    _fresh("always_pool")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    pol = policy.parse(_book(tmp), EMPLOYEES)
    st = pol.stage("t_top")
    from .autopilot import stages
    ctx = stages.Ctx(policy=pol, stage=st, crm=crm, run_id="t-pool", now=NOV, live=False,
                     out=stages.Summary(st.id, st.kind, st.owner, st.mode, False))
    o = top1000.options(st)
    p = top1000.tracks_dir() / "always-pool.json"
    top1000.cached_pool(ctx, o, p, list(st.presets), ["Top 1000"], ["Agency"], holder_tag="Top 1000")
    first = top1000.load_json(p, {})
    rows2 = top1000.cached_pool(ctx, o, p, list(st.presets), ["Top 1000"], ["Agency"], holder_tag="Top 1000", always=ALWAYS)
    check(first.get("always") == "" and "v1" not in (first.get("rows") or {})
          and (rows2 or {}).get("v1", {}).get("always") == ["tagged code_violation"]
          and (rows2 or {}).get("c1", {}).get("always") == ["on the Condemned list"]
          and top1000.load_json(p, {}).get("always") == top1000.always_key(ALWAYS),
          "a pool kept before the always-in titles were set is read again, and each record carries why it is in")
    ctx.out.tally.clear()
    top1000.cached_pool(ctx, o, p, list(st.presets), ["Top 1000"], ["Agency"], holder_tag="Top 1000", always=ALWAYS)
    check(any(k.startswith("pool rows (kept from") for k in ctx.out.tally), "under the same titles the kept pool is used")
    crm.list_uuid = lambda title: "" if title.strip().lower() == "no such list" else "list-" + title.strip().lower()
    top1000.pool_rows(ctx, o, list(st.presets), always={"lists": ["No Such List"], "tags": []})
    check(any("always_include names the list No Such List, which the account does not have" in x for x in ctx.out.problems),
          "a title the account does not hold is said, never read as a quiet month")

    # --- the playbook check
    for bad, why in (({"always_include": "Condemned"}, "a bare title instead of a mapping"),
                     ({"always_include": {"list": ["Condemned"]}}, "a key that is not lists or tags"),
                     ({"always_include": {"tags": "code_violation"}}, "a title that is not in a list"),
                     ({"always_include": {"lists": [""]}}, "an empty title")):
        try:
            policy.parse(_book(tmp, **bad), EMPLOYEES)
            check(False, "a block with %s is refused when the playbook loads" % why)
        except policy.PolicyError as exc:
            check("always_include" in str(exc), "a block with %s is refused when the playbook loads" % why)
    pb = _book(tmp)
    pb["stages"]["t_agency"]["always_include"] = {"lists": ["Condemned"]}
    try:
        policy.parse(pb, EMPLOYEES)
        check(False, "always_include on the agency stage is refused: only the Top 1,000 reads it")
    except policy.PolicyError as exc:
        check("read only by the Top 1,000 stage" in str(exc), "always_include on the agency stage is refused: only the Top 1,000 reads it")
    src = (Path(__file__).resolve().parent / "autopilot" / "top1000.py").read_text(encoding="utf-8")
    check(chr(0x2014) not in src and chr(0x2013) not in src, "the module carries no dash")
    _feeds(check, tmp)


class FeedCRM(BandCRM):
    """The fake account plus the one search key the daily pass leans on: must_not any_tags inside must
    (the live search nests the exclusion there), so a house that holds the tag is never read again."""

    def _match(self, r, must):
        if not super()._match(r, must):
            return False
        mn = must.get("must_not") or {}
        if mn.get("any_tags") and {x.replace("tag-", "") for x in mn["any_tags"]} & {t.lower() for t in r["tags"]}:
            return False
        return True


def feed_account() -> list:
    """The always-in account plus Liens records: N is a person with a lien pulled five days ago (new),
    O was pulled two months ago (backlog, left to the ranking), P is a new lien whose owner is Do Not
    Market on another house, Q a new lien owned by a company."""
    out = account()
    out += [
        house("n1", "21 Feed St", "o-n", lists=["Liens"], tags=["pulled_2026-11-06"]),
        house("o1", "22 Feed St", "o-o", lists=["Liens"], tags=["pulled_2026-09-10"]),
        house("p1", "23 Feed St", "o-p", lists=["Liens"], tags=["pulled_2026-11-06"]),
        house("p2", "24 Feed St", "o-p", tags=["Do Not Market"]),
        house("q1", "25 Feed St", "o-q", lists=["Liens"], tags=["pulled_2026-11-06"], first="", last="",
              company="Example Liens LLC"),
    ]
    return out


def _feeds(check, tmp: Path) -> None:
    """always_on_top, always_people_only and the daily county-feed pass (Ty, 2026-11-20)."""
    from .autopilot import ledger, policy, sweep, top1000
    print("  the county feeds on top of the Top 1,000, daily")
    # On top: the ranking fills every slot of size, the always-in owners come on top of it.
    _fresh("always_top")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    pol = policy.parse(_book(tmp, always_include=copy.deepcopy(ALWAYS), always_on_top=True), EMPLOYEES)
    sweep.run("t_top", crm=crm, now=NOV, pol=pol, plan=True)
    owners = top1000.month_state("t_top", "2026-11").get("owners") or []
    check({"o-c", "o-e", "o-m", "o-v"} <= set(owners) and {"o-r1", "o-r2", "o-r3"} <= set(owners),
          "always_on_top: the always-in owners come on top and the ranking still fills its own slots: %s" % owners)
    # People only, for the always-in houses too.
    _fresh("always_people")
    crm = BandCRM(account())
    top1000.search = OddsMap(crm)
    pol = policy.parse(_book(tmp, always_include=copy.deepcopy(ALWAYS), always_people_only=True), EMPLOYEES)
    r = sweep.run("t_top", crm=crm, now=NOV, pol=pol, plan=True)
    state = top1000.month_state("t_top", "2026-11")
    check("o-e" not in (state.get("always") or [])
          and r.tally.get("left out: always in, but the owner is held out whole: a company or trust, not a person") == 1,
          "always_people_only: the company on the eviction list is left out whole and counted: %s" % state.get("always"))

    def book(mode="plan", **over):
        b = _book(tmp, always_include=copy.deepcopy(ALWAYS), always_people_only=True)
        feed = {"kind": "top1000", "job": "include", "owner": "analysis", "mode": mode, "schedule": "07:35 daily",
                "lane": "top1000", "presets": ["Agency - 01 Queue"], "include_from": "t_top",
                "recent_lists": ["Liens"], "recent_days": 30, "include_reads": 50}
        feed.update(over)
        b["stages"]["t_feeds"] = feed
        return b

    # Plan: who would come in today, nothing written.
    _fresh("feeds_plan")
    crm = FeedCRM(feed_account())
    pol = policy.parse(book(), EMPLOYEES)
    r = sweep.run("t_feeds", crm=crm, now=NOV, pol=pol, plan=True)
    would = {ln.split(",")[0] for ln in r.lines}
    check(r.head.startswith("Would put the Top 1000 tag on 3 county record(s)") and not crm.writes,
          "the daily pass in plan says how many would come in and writes nothing: %s" % r.head)
    check({"7 Always St", "8 Always St", "21 Feed St"} <= would and not ({"22 Feed St", "23 Feed St", "25 Feed St",
                                                                            "4 Always St", "9 Always St"} & would),
          "in: the code-violation house, the mail-only condemned house and the lien pulled five days ago; out: the "
          "lien pulled two months ago (the ranking's), a lien whose owner is Do Not Market on another house, a "
          "company's lien, the house already tagged and the sold one: %s" % sorted(would))
    t = r.tally
    check(t.get("left out: a company or trust, not a person") == 2
          and t.get("left out: held on another house (a contact stop, a possible death or a higher lane's tag)") == 2
          and t.get("left out: tagged Do Not Market") == 1 and t.get("left out: a live lead") == 1
          and t.get("left out: already in the first-to-market lane") == 1,
          "each one left out is counted under its reason (the sold house already holds the tag, so it is never "
          "read): %s" % dict(t))
    # Live: the tags land, the first is read back, every change can be put back, a second run is quiet.
    _fresh("feeds_live")
    crm = FeedCRM(feed_account())
    config.MODE = "live"
    try:
        live = policy.parse(book(mode="live"), EMPLOYEES)
        sweep.run("t_feeds", crm=crm, now=NOV, pol=live)
        again = sweep.run("t_feeds", crm=crm, now=NOV, pol=live)
    finally:
        config.MODE = "dry"
    added = sorted(x[1] for x in crm.writes if x[0] == "add_tags")
    check(added == ["m1", "n1", "v1"] and all("Top 1000" in crm.recs[u]["tags"] for u in added)
          and not [x for x in crm.writes if x[0] == "remove_tags"],
          "live, the three get the tag and nothing is ever removed by the daily pass: %s" % added)
    check(len([c for c in ledger.changes() if c.get("stage") == "t_feeds"]) == 3,
          "every tag the daily pass adds is recorded with what it replaced, so undo can take it back")
    check(again.head.startswith("No new county record") and len([x for x in crm.writes if x[0] == "add_tags"]) == 3,
          "a second run the same morning finds nothing new and writes nothing: %s" % again.head)
    # A title the account does not hold is said, never a quiet day; and the playbook check knows the job.
    _fresh("feeds_missing")
    crm = FeedCRM(feed_account())
    crm.list_uuid = lambda title: "" if title.strip().lower() == "no such list" else "list-" + title.strip().lower()
    pol = policy.parse(book(recent_lists=["No Such List"]), EMPLOYEES)
    r = sweep.run("t_feeds", crm=crm, now=NOV, pol=pol, plan=True)
    check(any("recent_lists names the list No Such List" in p for p in r.problems),
          "a recent list the account does not hold is said: %s" % r.problems)
    for bad, why in (({"job": "monthly"}, "an unknown job"), ({"recent_lists": "Liens"}, "a bare title"),
                     ({"always_on_top": "yes"}, "a word for a true or false")):
        try:
            policy.parse(book(**bad), EMPLOYEES)
            check(False, "the daily pass with %s is refused when the playbook loads" % why)
        except policy.PolicyError:
            check(True, "the daily pass with %s is refused when the playbook loads" % why)


if __name__ == "__main__":
    raise SystemExit("run through: python src/agents/cli.py selftest")
