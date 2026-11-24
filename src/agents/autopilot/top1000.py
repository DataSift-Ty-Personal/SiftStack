"""The Top 1,000: the owners worked in-house beside first to market (the owner's decision, 2026-11-13).

TWO TRACKS. Every record in the buy box is in exactly one:
  Priority  every first-to-market record (tag FTM) plus the Top 1,000 owners (tag Top 1000), worked
            in-house through the whole sequence: Smarter Contact texts, the call round, monthly
            mail, then research. First to market sits on top of the 1,000.
  Agency    everything else in the buy box, sent once a month to a calling agency (agencyexport.py).
This stage keeps the tag Top 1000 on the right owners, once a month.

THE SCORE IS DREAM 100'S, NOT A COPY. Each house is scored by dream100_build.score(): deal size
(the seller's discount room), condition (the D4D visual score), investor, distress evidence and
reach. The D4D score exists only for the D4D cohort, so for every other house the condition points
are left out and the other components are scaled back to 100, and the run says how many owners
that was. Reach needs the owner's phones, which only the record itself holds, so it is known for
the finalists alone (below) and scaled out the same way until then.

RANK BY OWNER. One owner is one slot. An owner's score is their best house's score, with deal size
counted over every house they hold in the buy box: one conversation about three houses is worth
more than three rows. A selected owner gets the tag on every one of their houses still in the buy
box, so the owner sits in one lane (lanes.py).

WHO IS LEFT OUT, and said by reason: first-to-market records and anything else already in a higher
lane (a lead, an heir, a hot auction, an obituary), finished statuses, Do Not Market, a sold house,
an upside-down house, a house over $700,000, and anything the suppression gate would hold from a
caller (a litigator, an opt-out, a possible death, mail only). An owner who is a lead, opted out or
in a higher lane on ANY house is left out whole.

ALWAYS IN (the owner's ruling, 2026-11-17: condemned, code violation and eviction records go in the
Top 1,000 "no matter what"). The block's `always_include` names lists and tags; an owner with a house
on one of them takes a slot before the ranking fills the rest. Rank, the buy box, the discount-room
floor and the people-only rule do not apply to that house, so it is judged on its own full record,
and read even when the month's sweep never saw it. The hard holds still do (always_hold): a house in
a higher lane (first to market, an obituary, a live lead), sold, a finished status such as Not
Interested, Do Not Market, a litigator, an opt-out, a possible death, research under way, listed, not
a single-family house. A hold about the person leaves the owner out whole; mail only and the end of
the call round, which close one channel, hold nothing. The owner's other houses come in on the
ranking's own rules, so the owner sits in one lane. Unset, nobody is put in this way.

COUNTY FEEDS, DAILY (Ty, 2026-11-20: "make sure that the county feeds are all getting sent through
the smarter contact flow ... on the top 1,000 ... It doesn't have to be perfectly top 1,000 if there
are more properties that fit"). A stage of this kind with `job: include` runs every morning after the
first-to-market box's pull and tags the always-in houses that arrived since, so a county record
reaches the Top 1,000 texts the day it lands instead of on the 1st of next month. It reads its lists
from the monthly stage (`include_from`), judges each house with the same hard holds (always_hold),
writes only additions (the month's run keeps removing), and `recent_lists` brings in a list's NEW
arrivals only: a record on the list that carries a `pulled_<date>` tag from the last `recent_days`
(the county pull stamps one on every record it uploads), so the Liens backlog stays on the ranking
while each day's new liens go straight in. `always_on_top: true` puts the always-in owners on top
of `size` instead of inside it, and `always_people_only: true` keeps the people-only rule for them
too (the owner's rule of 2026-11-14), so a company or trust on a county list is left out.

WHERE THE NUMBERS COME FROM, and what it costs. SiftMap's documented search (POST
/properties/search/, in_my_account_mode in) returns, for every house already in the account, the
value, equity, the investor and realtor scores, the distress flags, the record's id and its tags
and lists: one request per 100 houses, never a detail read (DataSift's acceptable use forbids
detail pulls in volume without adding the records). It is swept once a month and shared with the
agency export. The owner of each house comes from the account's own search rows (the pool presets
in the stage block, 250 rows a request), kept for two days. Only the finalists' records are opened,
capped per run and remembered for a few days, so a run that stops on the hour's request budget
picks up where it was on the next slot.

PLAN AND LIVE. A plan run works the whole month out and writes nothing to the account: it says how
many houses would gain and lose the tag and leaves the ranking as a file. A live run adds and
removes the tag through the documented per-record routes, records each change with the value it
replaced (`cli.py autopilot undo <run>` puts a run back) and reads the first record of each kind
back before writing the rest. A holder that falls out of the top while its texts or call round are
under way keeps the tag until the round ends (keep_in_round), because cutting a sequence off between
two texts reads to the homeowner as a program that broke. The owner's first population can be run
on purpose without taking the stage live:

    python -m agents.autopilot.top1000 plan
    TOP1000_ALLOW=1 python -m agents.autopilot.top1000 apply --commit
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parents[2]
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would shadow modules of the same name (mail, policy). Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import config, store  # noqa: E402
from agents.autopilot import budget, gate, lanes, ledger, windows  # noqa: E402
from agents.autopilot.stages import Ctx, Summary, addr  # noqa: E402

ALLOW_ENV = "TOP1000_ALLOW"
TRACK = "top1000"
FACT_RECORD = "track_record"        # a finalist's record, pruned to what the gate, the score and the call list read
SPLIT_AT = 9000                     # a SiftMap search past 10,000 rows reorders; slices stay under it
SEARCH_WALL = 10000
BUY_BOX = {"type_single_family": True, "value_min": 1, "value_max": 700000, "extra_mls_status": ["not_listed"]}
# An exclusion of one of these kinds leaves the whole owner out: they are about the person, not the house.
OWNER_LEVEL = {"lane", "lead", "do_not_market", "litigator", "opted_out", "owner_lane", "deceased"}
# SiftMap's distress flags in Dream 100's list words, so its evidence points apply to a house whose
# list the account never attached. A flag with no Dream 100 list earns nothing.
FLAG_LISTS = {
    "is_notice_of_default": "Pre-Foreclosures - Notice of Default", "is_lis_pendens": "Pre-Foreclosures - Lis Pendens",
    "is_notice_of_foreclosure": "Foreclosure", "is_recent_deliquent": "Tax Delinquent", "is_vacant_property": "Vacant",
    "is_probate_properties": "Probate", "is_pre_probate": "Pre-Probate/Deceased", "is_heirs_property": "Pre-Probate/Deceased",
    "is_inheritance_property": "Pre-Probate/Deceased", "is_obituary_property": "Obituary", "is_estate_sales": "Estate Sales",
    "is_other_lien": "Liens", "is_state_tax": "Liens", "is_federal_lien": "Liens", "is_hoa_lien": "Liens",
    "is_judgment_liens": "Judgments", "is_final_judgment": "Judgments", "is_court_order": "Judgments",
    "is_bankruptcy_property": "Bankruptcy", "is_divorce_property": "Divorce", "is_tired_landlord": "Tired Landlord",
    "is_eviction": "Eviction", "is_bad_credit": "Low Credit Score", "is_low_income": "Low Income",
    "is_senior_homeowners": "Senior Homeowners",
}
TIER_ORDER = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3, "": 4, "Drop": 5}
DEFAULTS = {
    "size": 1000, "tag": "Top 1000", "finalists": 1.5, "max_reads": 1500, "max_writes": 2500,
    "max_searches": 900, "page_size": 100, "max_pool": 80000, "facts_days": 5, "pool_days": 2, "weights": "today",
    "keep_in_round": True, "pool_tags": [], "pool_lists": ["Agency"], "d4d_cohort": "", "counties": [],
    # An owner with a house on one of these lists belongs to another lane's work: the Obituary list is
    # the obituary lane and its research, which ranks above the Top 1,000. The Priority presets do not
    # exclude it (the first-to-market lane keeps it on purpose), so the selection itself must.
    "exclude_lists": ["Obituary"],
    # How owners are ranked. deal_odds (2026-11-14, the owner's ask: "as objectively as possible") is
    # the chance an investor buys one of the owner's houses within six months, learned from what sold
    # (src/deal_odds.py); dream100 is the old hand-set points, kept so the two can be compared.
    "score": "deal_odds", "odds_model": "",
    # A deal needs room for a fee: a house whose discount room (the smaller of its equity and 30% of
    # its value) is under this is left out whatever its odds.
    "min_room": 15000,
    # People only (the owner's rule, 2026-11-14: "we don't want to work with entities, just
    # individuals"): an owner the research gate would call an entity (a company field with no person's
    # name, an LLC, corporation, trust, church, bank or partnership in the name, or no full name) is
    # left out whole. Measured on the first deal-odds plan: 160 of the 1,000 were entities.
    "individuals_only": True,
    # Lists and tags whose records are in whatever their rank ({lists: [...], tags: [...]}, titles as
    # the account holds them; see ALWAYS IN above). Empty: nobody is put in this way.
    "always_include": {},
    # Always-in owners on top of `size` instead of taking its slots first (Ty, 2026-11-20: the Top 1,000
    # "doesn't have to be perfectly" 1,000 when more county records fit).
    "always_on_top": False,
    # The people-only rule for always-in houses too (a company or trust on a county list is left out).
    "always_people_only": False,
    # job: include (COUNTY FEEDS, DAILY above): the monthly stage whose always_include it reads, the lists
    # whose NEW arrivals only come in, how recent counts, and how much one run may read and write.
    "include_from": "top1000_refresh",
    "recent_lists": [],
    "recent_days": 30,
    "include_reads": 250,
    "include_recheck_days": 14,
    # The live Doors per Deal ladder paired with the deal odds (Ty, 2026-11-24: "pair that with the framework
    # to use for the top 1,000"): each house's odds times how much better or worse its list is selling now
    # than the published ladder says (dpdlive.list_factors). The odds stay the ranking (on the 2026-05
    # back-test they found 17 deals in their top 1,000 houses against 4 for the Priority 1 lists); a list's
    # measured sales move its houses up or down. 1.0 for every house until a list has sales of its own.
    "live_lists": False,
}
# A hold that closes only one channel keeps no always-in house out: mail only (it is still mailed) and
# the call round being over (it stays in the lane). Every other reason the gate gives against assigning
# the house does, as the owner's hand run of 2026-11-17 chose them (ledger run forcetopbc8fd2).
ALWAYS_SOFT = frozenset({"mail_only", "exhausted"})
ALWAYS_KINDS = ("lists", "tags")
POOL_VERSION = 2      # the pool rows carry the owner's entity verdict; a pool kept before is read again
SCORES = ("deal_odds", "dream100")
SLIM_VERSION = 2      # the month's sweep keeps what the deal-odds model reads; a sweep made before is re-made
# Tests swap this for a fake SiftMap. In production the search goes through the ladder check's
# transport, which can reach only the search route.
search = None


# ---------------------------------------------------------------------- settings
def options(st) -> dict:
    o = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k)
        if v is not None:
            o[k] = v
    o["counties"] = [dict(c) for c in (o.get("counties") or []) if isinstance(c, dict)]
    o["size"] = int(o["size"])
    return o


def block_problems(st) -> list[str]:
    """What is wrong with a Top 1,000 or agency block, in the playbook checker's words, so a typo
    fails `cli.py autopilot check` instead of the first night's run."""
    out = []
    for key, low in (("size", 1), ("max_reads", 0), ("max_writes", 0), ("max_searches", 1), ("page_size", 1),
                     ("max_pool", 1), ("facts_days", 0), ("pool_days", 0)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    v = st.get("finalists")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1):
        out.append("stage %s: finalists is how many owners past the size are read in full, as a multiple of it "
                   "(1.5 reads 1,500 for 1,000)" % st.id)
    ps = st.get("page_size")
    if isinstance(ps, int) and not isinstance(ps, bool) and ps > 100:
        out.append("stage %s: page_size above 100 is against SiftMap's fair use; keep it 100 or under" % st.id)
    counties = st.get("counties")
    include_job = str(st.get("job") or "").strip().lower() == "include"
    if include_job:
        pass                        # the daily county-feed job searches the account, never SiftMap
    elif not isinstance(counties, list) or not counties:
        out.append("stage %s: name the counties to search, as counties: [{fips, name, state}]" % st.id)
    elif any(not isinstance(c, dict) or not (c.get("fips") and c.get("name") and c.get("state")) for c in counties):
        out.append("stage %s: each county needs fips, name and state" % st.id)
    job = st.get("job")
    if job is not None and str(job).strip().lower() not in ("", "include"):
        out.append("stage %s: job is include (the daily county-feed pass) or left out (the monthly ranking)" % st.id)
    for key in ("always_on_top", "always_people_only", "live_lists"):
        v = st.get(key)
        if v is not None and not isinstance(v, bool):
            out.append("stage %s: %s is true or false" % (st.id, key))
    rl = st.get("recent_lists")
    if rl is not None and (not isinstance(rl, list) or any(not isinstance(x, str) or not x.strip() for x in rl)):
        out.append("stage %s: recent_lists must be a list of list titles" % st.id)
    for key, low in (("recent_days", 1), ("include_reads", 1), ("include_recheck_days", 0)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    if "tag" in (st.params or {}) and not str(st.get("tag") or "").strip():
        out.append("stage %s: tag may not be empty" % st.id)
    for key in ("exclude_lists", "pool_lists", "pool_tags"):
        v = st.get(key)
        if v is not None and not isinstance(v, list):
            out.append("stage %s: %s must be a list of titles" % (st.id, key))
    w = str(st.get("weights") or "today").strip().lower()
    if w not in ("today", "taught"):
        out.append("stage %s: weights must be today or taught (the Dream 100 weight sets)" % st.id)
    sc = str(st.get("score") or "deal_odds").strip().lower()
    if sc not in SCORES:
        out.append("stage %s: score must be %s" % (st.id, " or ".join(SCORES)))
    mr = st.get("min_room")
    if mr is not None and (isinstance(mr, bool) or not isinstance(mr, (int, float)) or mr < 0):
        out.append("stage %s: min_room is the smallest discount room in dollars, 0 or more" % st.id)
    io = st.get("individuals_only")
    if io is not None and not isinstance(io, bool):
        out.append("stage %s: individuals_only is true or false" % st.id)
    ai = st.get("always_include")
    if ai is not None:
        if st.kind != "top1000":
            out.append("stage %s: always_include is read only by the Top 1,000 stage (kind top1000)" % st.id)
        elif not isinstance(ai, dict):
            out.append("stage %s: always_include maps lists and tags to titles, as always_include: {lists: [...], tags: [...]}"
                       % st.id)
        else:
            bad = sorted(str(k) for k in ai if k not in ALWAYS_KINDS)
            if bad:
                out.append("stage %s: always_include has %s (its keys are lists and tags)" % (st.id, ", ".join(bad)))
            for k in ALWAYS_KINDS:
                v = ai.get(k)
                if v is not None and (not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() for x in v)):
                    out.append("stage %s: always_include.%s must be a list of titles" % (st.id, k))
    return out


def always_include(o: dict) -> dict:
    """The block's always_include as {"lists": [...], "tags": [...]}, each title trimmed; both empty when
    it is unset."""
    raw = (o or {}).get("always_include")
    raw = raw if isinstance(raw, dict) else {}
    return {k: [str(t).strip() for t in (raw.get(k) or []) if isinstance(t, str) and t.strip()] for k in ALWAYS_KINDS}


def always_key(always: dict | None) -> str:
    """The always-in titles in one comparable string ('' for none), kept with the pool so a pool read
    before the titles changed is read again."""
    a = {k: sorted((always or {}).get(k) or []) for k in ALWAYS_KINDS}
    return json.dumps(a, sort_keys=True) if any(a.values()) else ""


def always_words(kind: str, title: str) -> str:
    return ("on the %s list" if kind == "lists" else "tagged %s") % title


def is_entity(owner: dict) -> bool:
    """The research gate's own test (deep_prospect_batch.is_entity), so one definition of an entity
    owner holds across the system."""
    import deep_prospect_batch
    return deep_prospect_batch.is_entity(owner or {})


# ---------------------------------------------------------------------- the month, on disk
def month_of(ctx) -> str:
    return ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).strftime("%Y-%m")


def tracks_dir() -> Path:
    return Path(config.DATA_DIR) / "tracks"


def state_path(stage_id: str, month: str, mode: str) -> Path:
    return tracks_dir() / ("%s-%s-%s.json" % (stage_id, month, mode))


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, default=str), encoding="utf-8")
    tmp.replace(path)


def month_state(stage_id: str, month: str) -> dict:
    """This month's state for a stage: the live run's when there is one, else the plan's."""
    for mode in ("live", "plan"):
        s = load_json(state_path(stage_id, month, mode), None)
        if isinstance(s, dict) and s:
            return s
    return {}


def files_dir(ctx) -> Path:
    """Where this run's files go: RUNS_DIR/<stage>-<task id>/files, which the brain serves by a
    signed link because the run's result names the folder (Summary.run_dir)."""
    d = Path(config.RUNS_DIR) / ("%s-%s" % (ctx.stage.id, ctx.task_id or "cli")) / "files"
    d.mkdir(parents=True, exist_ok=True)
    ctx.out.run_dir = str(d.parent)
    return d


def link(ctx, path: Path) -> str:
    """A signed link to a run file when the run is a task on a box with a public address, else the path."""
    if ctx.task_id:
        from agents import slack
        url = slack.file_url(int(ctx.task_id), Path(path).name)
        if url:
            return url
    return str(path)


# ---------------------------------------------------------------------- the SiftMap sweep (shared by both tracks)
def _search(crm, body: dict) -> dict:
    if search is not None:
        return search(crm, body)
    from agents.autopilot import laddercheck
    return laddercheck._map_search(crm, body)


def mls_word(row: dict) -> str:
    if row.get("mlsListed") or row.get("mlsActive"):
        return "Listed"
    if row.get("mlsPending"):
        return "Pending"
    return ""


def slim_row(row: dict) -> dict:
    """The fields of a SiftMap search row the scores and the exclusions read. Since SLIM_VERSION 2 it
    also keeps what the deal-odds model reads (size, lot, place, a cash purchase, free and clear,
    high equity) and the on-market AI score, so a month's AI scores exist later to be tested against
    what sold."""
    import deal_odds
    d = row.get("distressors") or {}
    flags = sorted(k for k, v in d.items() if (v.get("is_active") if isinstance(v, dict) else v))
    pre = row.get("presets") or {}
    return {"v": row.get("estimatedValue"), "e": row.get("equityPercent"), "i": row.get("investor_off_market_score"),
            "r": row.get("realtor_score"), "abs": bool(row.get("absenteeOwner")), "corp": bool(row.get("corporateOwned")),
            "use": row.get("propertyUse") or "", "mls": mls_word(row), "tags": list(row.get("saved_tags") or []),
            "lists": list(row.get("saved_lists") or []), "flags": flags, "vac": bool(pre.get("vacant")),
            "addr": row.get("address") or "", "fips": str(row.get("fips") or ""), "id": str(row.get("id") or ""),
            "sq": row.get("squareFeet"), "bd": row.get("bedrooms"), "ba": row.get("bathrooms"), "lot": row.get("lotAcres"),
            "lat": row.get("latitude"), "lon": row.get("longitude"), "cash": bool(pre.get("cash_buyer")),
            "fc": bool(pre.get("free_clear")), "he": bool(pre.get("high_equity")), "zip": deal_odds.zip_of(row.get("address")),
            "ion": row.get("investor_on_market_score"), "mc": bool(row.get("mlsCancelled"))}


class _Asked:
    def __init__(self, cap: int):
        self.cap, self.n = int(cap), 0


def _sweep_slice(ctx, asked: _Asked, county: dict, filters: dict, lo: int, hi: int, page: int, out: dict,
                 stats: Counter, members: set | None = None) -> bool:
    """Every row in one county and one value band, bisected until a band holds fewer than SPLIT_AT
    rows. False when the run's search allowance ran out first. With `members`, only each row's record
    id is kept there (a flag's members, see _rebuild_flags)."""
    from agents.autopilot import laddercheck
    f = dict(filters, value_min=int(lo), value_max=int(hi))
    offset, total, got = 0, None, 0
    while True:
        if asked.n >= asked.cap:
            return False
        asked.n += 1
        body = {"result_index": 1 + offset, "size": int(page), "with_boundaries": False, "filters": f,
                "addresses": [laddercheck.county_address(county)], "polygon": laddercheck.WORLD}
        r = _search(ctx.crm, body) or {}
        if total is None:
            total = int(r.get("total_results") or 0)
            if total > SPLIT_AT and hi - lo >= 2:
                mid = (int(lo) + int(hi)) // 2
                stats["value bands split"] += 1
                return (_sweep_slice(ctx, asked, county, filters, lo, mid, page, out, stats, members)
                        and _sweep_slice(ctx, asked, county, filters, mid + 1, hi, page, out, stats, members))
            if total > SEARCH_WALL:
                stats["rows past the search wall, not read"] += total - SEARCH_WALL
        rows = r.get("data") or []
        for row in rows:
            u = row.get("saved_uuid")
            if not (row.get("saved") or u) or not u:
                stats["rows not in the account"] += 1
                continue
            if members is not None:
                members.add(u)
                continue
            if "distressors" in row:
                stats["rows carrying flags"] += 1
            out[u] = slim_row(row)
        got += len(rows)
        offset += len(rows)        # the next page starts after the rows read, whatever size a page is
        if not rows or got >= min(total, SEARCH_WALL):
            stats["rows read"] += got
            return True


def _rebuild_flags(ctx, asked: _Asked, o: dict, filters: dict, rows: dict, stats: Counter) -> bool:
    """SiftMap stopped returning each search row's flags, presets and AI scores (rows read 2026-11-14
    carried them; rows read from 2026-11-23 on do not, from the workstation and the brain alike), while
    its filters still work. So the flags the deal-odds model reads (deal_odds.MODEL_FLAGS: vacancy and tax
    delinquency carry most of its weight) are rebuilt by filter: one in-account search per flag, each
    member's row marked. A month's sweep made without them would rank every house as flag-free, silently.
    False when the run's search allowance ran out first."""
    import deal_odds
    for c in o["counties"]:
        for key in deal_odds.MODEL_FLAGS:
            got: set = set()
            f = dict(filters, distressors_include=key, distressors_include_min_match=1)
            if not _sweep_slice(ctx, asked, c, f, BUY_BOX["value_min"], BUY_BOX["value_max"], o["page_size"], {}, stats, got):
                return False
            for u in got & rows.keys():
                fl = rows[u].setdefault("flags", [])
                if key not in fl:
                    fl.append(key)
                    fl.sort()
                stats["flags rebuilt by filter"] += 1
    stats["flags searched by filter"] += len(deal_odds.MODEL_FLAGS) * len(o["counties"])
    ctx.out.tally["house flags rebuilt by filter (SiftMap rows no longer carry them)"] = stats["flags rebuilt by filter"]
    return True


def sweep_exclusions(ctx) -> list[str]:
    """The tag ids the month's sweep leaves out: the sold tags only, the same for both track stages
    so they share one sweep. A sold house is out on its own and there are tens of thousands of
    them. Everything that leaves a whole OWNER out (a first-to-market notice, Do Not Market, a
    litigator) must come back in the sweep, or the owner's other house would look clean."""
    gcfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    return sorted({u for u in (ctx.crm.tag_uuid(t) for t in gcfg.sold_tags) if u})


def sweep_key(counties: list[dict], exclude: list[str]) -> str:
    raw = json.dumps({"counties": [(c.get("fips"), c.get("name"), c.get("state")) for c in counties],
                      "box": BUY_BOX, "exclude": sorted(exclude), "slim": SLIM_VERSION}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def month_sweep(ctx, o: dict) -> dict | None:
    """record uuid -> slim SiftMap row for every house already in the account inside the buy box in
    the block's counties. Swept once a month into tracks/siftmap-<month>.json and shared by both
    track stages, so the agency export never pays for the same searches again. None when the sweep
    could not finish; the run says why and the next run starts it again."""
    month = month_of(ctx)
    path = tracks_dir() / ("siftmap-%s.json" % month)
    exclude = sweep_exclusions(ctx)
    key = sweep_key(o["counties"], exclude)
    have = load_json(path, {})
    if isinstance(have, dict) and have.get("key") == key and isinstance(have.get("rows"), dict):
        ctx.out.tally["houses in the month's SiftMap sweep"] = len(have["rows"])
        return have["rows"]
    if not o["counties"]:
        ctx.out.problem("no county is named in the block (counties: [{fips, name, state}]), so nothing was searched")
        return None
    filters = dict(BUY_BOX, in_my_account_mode="in")
    if exclude:
        filters["in_my_account_tags_exclude"] = list(exclude)
    rows, stats = {}, Counter()
    asked = _Asked(o["max_searches"])
    try:
        for c in o["counties"]:
            if not _sweep_slice(ctx, asked, c, filters, BUY_BOX["value_min"], BUY_BOX["value_max"], o["page_size"], rows, stats):
                ctx.out.problem("the SiftMap sweep needs more than %d searches (raise max_searches); it starts again "
                                "next run" % asked.cap)
                return None
        if rows and not stats["rows carrying flags"] and not _rebuild_flags(ctx, asked, o, filters, rows, stats):
            ctx.out.problem("the SiftMap sweep needs more than %d searches to rebuild the houses' flags (raise "
                            "max_searches); it starts again next run" % asked.cap)
            return None
    except Exception as exc:  # noqa: BLE001  (a SiftMap error is said; the next run starts the sweep again)
        ctx.out.problem("the SiftMap search failed (%s); the sweep starts again next run" % str(exc)[:160])
        return None
    seen = stats["rows read"]
    if seen and stats["rows not in the account"] > 0.10 * seen:
        ctx.out.problem("%d of %d SiftMap rows are not in the account, so the in-account filter did not hold; "
                        "the rows outside it were dropped" % (stats["rows not in the account"], seen))
    if stats["rows past the search wall, not read"]:
        ctx.out.problem("%d SiftMap rows sit past the search's 10,000-row wall in a band too narrow to split; they were not read"
                        % stats["rows past the search wall, not read"])
    save_json(path, {"key": key, "at": ctx.now.isoformat(timespec="seconds"), "rows": rows,
                     "stats": dict(stats, searches=asked.n)})
    ctx.out.tally["SiftMap searches"] = asked.n
    ctx.out.tally["houses in the month's SiftMap sweep"] = len(rows)
    return rows


# ---------------------------------------------------------------------- the account's own rows (the owner of each house)
def _with_range(must: dict, key: str, lo, hi) -> dict | None:
    """`must` with `key` held to [lo, hi], inside any range the preset already sets on it (a band
    outside the preset's own range would add records the preset leaves out). None when they do not meet."""
    m = dict(must)
    have = m.get(key)
    if isinstance(have, (list, tuple)) and len(have) == 2:
        a, b = have
        if a is not None:
            lo = max(lo, a)
        if b is not None:
            hi = min(hi, b)
    if lo > hi:
        return None
    m[key] = [lo, hi]
    return m


def band_windows(today) -> tuple[list, list]:
    """The deal-odds model's tenure and year-built bands as record-search ranges: last_sold dates and
    year-built years. A record with no last sale, or no year, falls in no band and reads unknown,
    exactly as a house with no sale on record does in training."""
    import datetime as _dt
    import deal_odds

    def ago(years):
        try:
            return today.replace(year=today.year - int(years))
        except ValueError:                       # 29 February
            return today.replace(year=today.year - int(years), day=28)
    tenure = []
    for lo, hi, name in deal_odds.TENURE_BANDS:
        start = _dt.date(1900, 1, 1) if hi == float("inf") else ago(hi) + timedelta(days=1)
        tenure.append((name, start.isoformat(), ago(lo).isoformat()))
    years = [(name, int(max(lo, 1700)), int(min(hi, 2101)) - 1) for lo, hi, name in deal_odds.YEAR_BANDS]
    return tenure, years


def band_rows(ctx, sources: list, out: dict) -> None:
    """Each pool record's tenure band and year-built band, written onto its pool row as `ten` and `yb`.
    The search rows carry neither, and both are among the strongest signals the model has, so each
    source is scanned once per band (the records search filters last_sold and year; both proven by
    count delta). Every record falls in at most one band, so the bands cost about one more pass of
    the pool each, and they are kept with the pool for pool_days."""
    from agents.autopilot.lanecheck import scan
    tenure, years = band_windows(ctx.now.date())
    hit = Counter()
    for label, must, *_rest in sources:
        for key, field, bands in (("ten", "last_sold", tenure), ("yb", "year", years)):
            for name, lo, hi in bands:
                m = _with_range(must, field, lo, hi)
                if m is None:
                    continue
                for r in scan(ctx, m):
                    u = r.get("uuid")
                    if u in out:
                        out[u][key] = name
                        hit[key] += 1
    have_ten = sum(1 for r in out.values() if r.get("ten"))
    have_yb = sum(1 for r in out.values() if r.get("yb"))
    ctx.out.tally["pool rows with a tenure band"] = have_ten
    ctx.out.tally["pool rows with a year-built band"] = have_yb
    if out and have_ten == 0 and have_yb == 0:
        ctx.out.problem("the band searches found no record in any tenure or year-built band, so the search may "
                        "no longer honor last_sold or year; every house was scored with both unknown")


def pool_rows(ctx, o: dict, presets: list[str], tag_titles=(), list_titles=(), holder_tag: str = "",
              bands: bool = False, always: dict | None = None) -> dict | None:
    """record uuid -> {owner, status, street, city, holds} from the stage's pool presets, the records
    carrying the pool tags and the records on the pool lists. A search row is the only place the
    owner's id is, and a page of 250 costs one request. None when a source is larger than the
    block's max_pool allows (the run says which). With `bands`, each row also gets its tenure and
    year-built band (band_rows). With `always` ({lists, tags}), the records on those lists and tags
    come in too, each carrying `always`: the words for why it is in whatever its rank."""
    from agents.autopilot.lanecheck import scan
    out: dict = {}
    room = int(o["max_pool"])
    sources = []
    for title in presets:
        try:
            must, _ = windows.resolve(ctx.crm.preset_must(title), ctx.now)
        except KeyError as exc:
            ctx.out.problem("%s (the pool skipped it)" % exc)
            continue
        sources.append(("preset %s" % title, must, False, ""))
    for t in tag_titles:
        uid = ctx.crm.tag_uuid(t)
        if uid:
            sources.append(("tag %s" % t, {"any_tags": [uid]}, bool(holder_tag) and t.strip().lower() == holder_tag.strip().lower(), ""))
        elif holder_tag and t.strip().lower() == holder_tag.strip().lower():
            ctx.out.tally["the tag %s does not exist yet: nobody holds it" % t] += 1
    for t in list_titles:
        uid = ctx.crm.list_uuid(t)
        if uid:
            sources.append(("list %s" % t, {"any_lists": [uid]}, False, ""))
    for kind in ALWAYS_KINDS:
        for t in (always or {}).get(kind) or []:
            uid = ctx.crm.list_uuid(t) if kind == "lists" else ctx.crm.tag_uuid(t)
            word = "list" if kind == "lists" else "tag"
            if not uid:
                # A title the account does not hold puts nobody in, which reads exactly like a month
                # with no such record: said, so a typo cannot pass for a quiet month.
                ctx.out.problem("always_include names the %s %s, which the account does not have; nobody was put in "
                                "for it" % (word, t))
                continue
            sources.append(("always_include %s %s" % (word, t), {"any_lists" if kind == "lists" else "any_tags": [uid]},
                            False, always_words(kind, t)))
    for label, must, holds, why in sources:
        n = ctx.crm.count(must)
        if n > room:
            ctx.out.problem("the pool's %s holds %d records, more than the %d left under max_pool; raise max_pool" % (label, n, room))
            return None
        rows = scan(ctx, must)
        room -= len(rows)
        try:
            # Knowledge, kept in plan mode too: a search row is the only place the do-not-call registry
            # flag shows (one number a row), and the agency file holds a flagged number (decision D32).
            from agents.autopilot import scrub
            scrub.note_rows(rows, lane=str(ctx.stage.get("lane") or ""), now=ctx.now)
        except Exception as exc:  # noqa: BLE001  (a failed note costs the registry hold its knowledge, and is said)
            ctx.out.problem("the registry flags on the %s rows could not be remembered (%s)" % (label, str(exc)[:80]))
        for r in rows:
            u = r.get("uuid")
            if not u:
                continue
            a = r.get("address") or {}
            have = out.get(u) or {"owner": (r.get("owner") or {}).get("uuid") or "", "status": lanes.status_of(r),
                                  "street": (a.get("street") if isinstance(a, dict) else str(a)) or "",
                                  "city": (a.get("city") if isinstance(a, dict) else "") or "", "holds": False,
                                  "src": label, "from": [], "entity": is_entity(r.get("owner") or {})}
            have["holds"] = bool(have["holds"] or holds)
            if label not in have["from"]:
                have["from"].append(label)
            if why and why not in have.setdefault("always", []):
                have["always"].append(why)
            out[u] = have
        ctx.out.tally["pool rows from the %s" % label] = len(rows)
    if bands and out:
        band_rows(ctx, sources, out)
    return out


def facts_of(key: str) -> dict:
    """record uuid -> (value, when it was written) for one fact key, in one query. A pool of forty
    thousand records asked one at a time would open the database forty thousand times."""
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT record_uuid, value, at FROM ap_facts WHERE key=?", (key,)).fetchall():
            try:
                out[r["record_uuid"]] = (json.loads(r["value"]), r["at"])
            except ValueError:
                out[r["record_uuid"]] = (r["value"], r["at"])
    return out


def cached_pool(ctx, o: dict, path: Path, presets, tag_titles=(), list_titles=(), holder_tag: str = "",
                always: dict | None = None) -> dict | None:
    """The pool, read once and kept for `pool_days`: a run that continues reading the next night
    must not page the account again to learn the same owners. A pool kept without the bands the
    deal-odds score needs is read again, and so is one kept under other always-in titles."""
    bands = score_of(o) == "deal_odds"
    akey = always_key(always)
    have = load_json(path, {})
    try:
        fresh = ctx.now - datetime.fromisoformat(have.get("at") or "") < timedelta(days=float(o["pool_days"]))
    except (TypeError, ValueError):
        fresh = False
    if (fresh and isinstance(have.get("rows"), dict) and (have.get("bands") or not bands)
            and int(have.get("v") or 1) >= POOL_VERSION and str(have.get("always") or "") == akey):
        ctx.out.tally["pool rows (kept from %s)" % str(have["at"])[:10]] = len(have["rows"])
        return have["rows"]
    rows = pool_rows(ctx, o, presets, tag_titles, list_titles, holder_tag, bands=bands, always=always)
    if rows is not None:
        save_json(path, {"at": ctx.now.isoformat(timespec="seconds"), "rows": rows, "bands": bands, "v": POOL_VERSION,
                         "always": akey})
    return rows


# ---------------------------------------------------------------------- the score
# The run's deal-odds model, set by scoring() and cleared by restore(), so candidates() and
# owner_score() (shared with the agency export) see the same model without a new argument.
_ODDS: dict = {"model": None, "min_room": 0.0, "live": {}}
# The run's owner rules, set by scoring() for either score and reset by restore().
_RULES: dict = {"individuals_only": True}


def score_of(o: dict) -> str:
    return str(o.get("score") or "deal_odds").strip().lower()


def scoring(o: dict):
    """dream100_build with the block's weight set on it (its buy-box rules and discount room are used
    by both scores), plus the deal-odds model when the block ranks on it. Imported here, not at the
    top: it carries the workbook code and the D4D client, which a playbook check should not have to
    load. A deal-odds block with no model file is refused, never quietly ranked on the old points."""
    import dream100
    import dream100_build as build
    dream100.apply_weights(str(o.get("weights") or "today"), build)
    io = o.get("individuals_only")
    _RULES["individuals_only"] = True if io is None else bool(io)
    if score_of(o) == "deal_odds":
        import deal_odds
        try:
            _ODDS["model"] = deal_odds.load(o.get("odds_model") or None)
        except (OSError, ValueError) as exc:
            restore(build)
            raise RuntimeError("the deal-odds model could not be read (%s); fit it with python src/deal_odds_train.py, "
                               "or set score: dream100 on the stage" % str(exc)[:120])
        _ODDS["min_room"] = float(o.get("min_room") if o.get("min_room") is not None else DEFAULTS["min_room"])
        if o.get("live_lists"):
            from agents.autopilot import dpdlive
            _ODDS["live"] = dpdlive.list_factors([str(c.get("fips") or "") for c in o.get("counties") or []])
    return build


def restore(build) -> None:
    import dream100
    dream100.apply_weights(dream100.DEFAULT_WEIGHTS, build)
    _ODDS["model"], _ODDS["min_room"], _ODDS["live"] = None, 0.0, {}
    _RULES["individuals_only"] = True


def live_odds(odds: float, s: dict | None, r: dict) -> float:
    """A house's odds times its list's live factor (the live_lists option), the list and factor kept on the
    house for the ranking file. Never above 0.99: a factor moves a chance, it does not make one certain."""
    got = (_ODDS.get("live") or {}).get(str((s or {}).get("id") or ""))
    if not got:
        return odds
    factor, label = got
    r["list_now"] = {"list": label, "factor": factor}
    return min(0.99, float(odds) * float(factor))


def odds_reach(model: dict, word: str) -> float:
    """How much likelier an owner is to be reached on their best number than on an unscored one, from
    the model's measured reach table (the account's own call log); 1 where nothing was measured."""
    table = (model or {}).get("reach") or {}
    base = float(table.get("untiered") or 0) or None
    rate = table.get(word)
    if not base or rate is None:
        return 1.0
    return float(rate) / base


def odds_owner_score(model: dict, houses: list[dict], reach_word: str | None = None) -> dict:
    """An owner on deal odds: the chance at least one of their houses in the pool sells to an investor
    within six months, times how reachable they are (1 until their phones are read). `score` is
    that chance in percent, so the CSV reads as odds, not points."""
    import deal_odds
    p = deal_odds.owner_prob(float(h["odds"]) for h in houses)
    f = odds_reach(model, reach_word or "")
    best = max(houses, key=lambda h: (float(h["odds"]), h["uuid"]))
    room = sum(float(h.get("deal_size") or 0) for h in houses)
    return {"score": round(100.0 * p * f, 3), "odds": p, "reach_factor": round(f, 3), "best": best["uuid"],
            "room": round(room, -2), "pts": {"odds": round(100.0 * p, 3), "reach": round(f, 3)}, "missing": [],
            "why": deal_odds.reasons(model, best.get("facts") or {}) if best.get("facts") is not None else []}


def weights(build) -> dict:
    return {"deal": float(build.W_DEAL), "cond": float(build.W_COND), "inv": float(build.W_INV),
            "evid": float(build.W_EVID), "reach": float(build.W_REACH)}


def d4d_scores(o: dict) -> dict:
    """record uuid -> the D4D visual distress score, from the cohort file when the box has one (the
    block's d4d_cohort, else DATA_DIR/d4d/cohort.json, else the repo's output/d4d_call_list/cohort.json)."""
    paths = [o.get("d4d_cohort")] if o.get("d4d_cohort") else []
    paths += [str(Path(config.DATA_DIR) / "d4d" / "cohort.json"), str(SRC.parent / "output" / "d4d_call_list" / "cohort.json")]
    for p in paths:
        rows = load_json(Path(p), None)
        if isinstance(rows, list):
            return {str(r["saved_uuid"]): r["score"] for r in rows
                    if isinstance(r, dict) and r.get("saved_uuid") and r.get("score") is not None}
    return {}


def _num(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def house(uuid: str, s: dict, tags: list[str], lists: list[str], status: str, d4d: dict) -> dict:
    """One house in the shape dream100_build.score() reads, from the SiftMap row and the titles of
    its tags and lists. What a search row cannot know (a tax amount, an owner's age) stays empty
    and earns nothing, exactly as Dream 100 treats a blank field."""
    v, e = _num(s.get("v")), _num(s.get("e"))
    named = set(lists) | {FLAG_LISTS[f] for f in s.get("flags") or [] if f in FLAG_LISTS}
    if s.get("abs"):
        named.add("Absentee Owners")
    return {"uuid": uuid, "street": str(s.get("addr") or "").split(",")[0].strip(), "value": v, "equity_pct": e,
            "equity_dollars": v * e / 100.0 if v is not None and e is not None else None,
            "d4d_score": d4d.get(uuid), "d4d_rank": None, "investor_score": _num(s.get("i")), "realtor_score": _num(s.get("r")),
            "lists": sorted(named), "tags": list(tags), "vacant": "is_vacant_property" in (s.get("flags") or []) or bool(s.get("vac")),
            "tax_owed": None, "deceased": False, "foreclosure_date": "", "auction_date": "", "probate_open": "",
            "bankruptcy_date": "", "divorce_date": "", "age": None, "years_held": None, "out_of_state": False,
            "dnc": False, "best_tier": "", "n_phones": 0, "status": status or "", "status_l": (status or "").lower(),
            "attempts": 0, "entity": bool(s.get("corp")), "do_not_mail": False, "structure": s.get("use") or "",
            "mls": s.get("mls") or ""}


def owner_score(build, houses: list[dict], reach_pts: float | None = None, reach_word: str | None = None) -> dict:
    """The owner's score. On deal odds (the default since 2026-11-14) it is odds_owner_score. On the
    old Dream 100 points it is their best house's Dream 100 score, with deal size counted over every
    house they hold in the buy box; a component the data does not have (condition with no D4D score,
    reach before the record is read) is left out and the rest are scaled back to 100."""
    if _ODDS["model"] is not None and houses and all("odds" in h for h in houses):
        if reach_word is None and reach_pts is not None:
            reach_word = next((t for t, v in build.TIER_PTS.items() if t and float(v) == float(reach_pts)), "")
        return odds_owner_score(_ODDS["model"], houses, reach_word)
    W = weights(build)
    room = sum(float(h.get("deal_size") or 0) for h in houses)
    deal = W["deal"] * build.ramp(room, build.DEAL_LO, build.DEAL_HI)
    best = None
    for h in houses:
        pts = {"deal": deal, "inv": float(h["pts"]["inv"]), "evid": float(h["pts"]["evid"])}
        if h.get("d4d_score") is not None:
            pts["cond"] = float(h["pts"]["cond"])
        if reach_pts is not None:
            pts["reach"] = float(reach_pts)
        s = 100.0 * sum(pts.values()) / max(1e-9, sum(W[k] for k in pts))
        if best is None or s > best[0] + 1e-9:
            best = (s, h, pts)
    s, h, pts = best
    return {"score": round(s, 1), "best": h["uuid"], "room": round(room, -2),
            "pts": {k: round(v, 1) for k, v in pts.items()}, "missing": [k for k in ("cond", "reach") if k not in pts]}


def reach_of(build, rec: dict) -> tuple[float, str]:
    """Reach points and words from the owner's phones, Dream 100's way: the best dial tier on a number
    not marked wrong, dead or do-not-call; an owner who opted out is mail only."""
    ow = (rec or {}).get("owner") or {}
    if ow.get("dnc") or ow.get("opt_out"):
        return 0.0, "DNC, mail only"
    phones = ow.get("phones") or []
    tiers = [gate.phone_tier(p) for p in phones if str(p.get("status") or "UNKNOWN").upper() not in gate.BAD_PHONE]
    best = min(tiers, key=lambda t: TIER_ORDER.get(t, 4)) if tiers else ""
    return float(build.TIER_PTS.get(best, 0)), best or ("phones untiered" if phones else "no phone")


# ---------------------------------------------------------------------- exclusions
def index(crm, kind: str) -> dict:
    """uuid -> title for every tag or every list, read once per client (two requests a run). The
    titles are kept as the account spells them: Dream 100's list points are keyed on that spelling.
    The cache lives on the client, so a fresh client (every stage run) reads a fresh index."""
    cache = getattr(crm, "_track_index", None)
    if cache is None:
        cache = {}
        try:
            setattr(crm, "_track_index", cache)
        except AttributeError:
            pass
    if kind not in cache:
        from agents.autopilot.crm import rows_of
        path = "/api/internal/tag/?offset=0&limit=10000&ordering=title" if kind == "tag" else "/api/internal/list/?offset=0&limit=1000"
        try:
            rows = rows_of(crm.call(path))
            cache[kind] = {r.get("uuid"): str(r.get("title") or "").strip() for r in rows or [] if r.get("uuid")}
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001  (an index that cannot be read leaves the titles unknown, and the run says so)
            cache[kind] = None
    return cache[kind]


def titles_for(crm, uuids, kind: str) -> list[str]:
    idx = index(crm, kind) or {}
    return [idx[u] for u in uuids or [] if u in idx]


def on_held_list(lists, held) -> str:
    """The first of the record's lists that keeps its owner out of the tracks (exclude_lists), or ''."""
    want = {str(x).strip().lower() for x in held or ()}
    return next((str(t) for t in lists or () if str(t).strip().lower() in want), "")


def exclusion(ctx, uuid: str, tags: list[str], lists: list[str], status: str, owner_uuid: str, target: str,
              r: dict | None = None, build=None, held_lists=()) -> tuple[str, str]:
    """(code, words) for why a house is left out of a track, or ("", "") when it is in. Asked of the
    lanes table first (a house already in a higher lane belongs to it), then of the block's own
    held lists (the Obituary list), then of the suppression gate as a hand-off to a caller, then
    of Dream 100's own buy-box rules."""
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    pseudo = {"uuid": uuid, "status": status or None, "tags": list(tags), "lists": list(lists), "owner": {}}
    mine = lanes.lane_of(pseudo, cfg)
    if lanes.rank(mine) < lanes.rank(target):
        if mine == "lead":
            return "lead", "a live lead"
        return "lane", "already in the %s lane" % lanes.name(mine)
    held = on_held_list(lists, held_lists)
    if held:
        return "lane", "on the %s list" % held
    v = gate.check(pseudo, "assign", cfg=getattr(ctx.policy, "gate", None) or gate.GateConfig(), lane=target,
                   owner=lanes.owner_lane(owner_uuid) if owner_uuid else None, lanes_cfg=cfg)
    if not v.ok:
        return v.code, v.reason()
    if r is not None and build is not None:
        why = build.gate_reason(r)
        if why:
            return "buy_box", why[0].lower() + why[1:]
    return "", ""


def candidates(ctx, build, sift: dict, pool: dict, target: str, d4d: dict, skip=None,
               held_lists=(), always=frozenset(), dropped_out: dict | None = None) -> tuple[dict, Counter]:
    """owner key -> {"houses": [...], "pre": owner_score without reach} for every owner with a house
    still in, and the houses left out, counted by reason. `skip(uuid, owner)` returns a reason to
    leave a house out before it is scored (the agency's 90-day rule), or ''. A house on a held list
    (exclude_lists) leaves its whole owner out. `always` names the always-in houses: each is decided
    on its own record (always_in), so it is neither ranked nor counted here, and the people-only rule
    is lifted for its owner's other houses (the owner is in whatever they are). `dropped_out`, when
    given, receives owner key -> why for every owner left out whole."""
    out_by: Counter = Counter()
    owners: dict = {}
    dropped: dict = {}
    always_owners = {(pool.get(u) or {}).get("owner") or u for u in always}
    for u, p in pool.items():
        if u in always:
            continue
        s = sift.get(u)
        if s is None:
            out_by["no SiftMap row in the buy box (outside the counties, the value or single family, or tagged out)"] += 1
            continue
        key = p.get("owner") or u
        entity = bool(_RULES["individuals_only"] and p.get("entity"))
        if entity and key not in always_owners:
            out_by["the owner is a company or trust, not a person"] += 1
            continue
        if skip is not None:
            why = skip(u, key)
            if why:
                out_by[why] += 1
                continue
        tags = titles_for(ctx.crm, s.get("tags"), "tag")
        lists = titles_for(ctx.crm, s.get("lists"), "list")
        r = house(u, s, tags, lists, p.get("status") or "", d4d)
        code, why = exclusion(ctx, u, tags, lists, p.get("status") or "", p.get("owner") or "", target, r, build,
                              held_lists)
        if code:
            out_by[why] += 1
            if code in OWNER_LEVEL:
                dropped.setdefault(key, why)
            continue
        build.score(r)
        model = _ODDS["model"]
        if model is not None:
            if float(r.get("deal_size") or 0) < _ODDS["min_room"]:
                out_by["under $%s of discount room (no room for a fee)" % format(int(_ODDS["min_room"]), ",")] += 1
                continue
            import deal_odds
            facts = deal_odds.with_bands(deal_odds.facts_from_search_row(s), p.get("ten"), p.get("yb"))
            r["facts"] = facts
            r["odds"] = live_odds(deal_odds.prob(model, facts), s, r)
        r["holds"] = bool(p.get("holds"))
        r["city"] = p.get("city") or ""
        owners.setdefault(key, {"houses": []})["houses"].append(r)
        if entity:
            # An always-in owner's other house, kept for always_in. Should every always-in house of the
            # owner be held, the owner is ranked like anyone else, and the people-only rule holds (_work).
            owners[key]["entity"] = True
    for key, why in dropped.items():
        if key in owners:
            out_by["owner left out whole: %s on another house" % why] += len(owners[key]["houses"])
            del owners[key]
    if dropped_out is not None:
        dropped_out.update(dropped)
    for o in owners.values():
        o["pre"] = owner_score(build, o["houses"])
    return owners, out_by


# ---------------------------------------------------------------------- always in
def always_hold(ctx, rec: dict, held_lists=()) -> tuple[str, str]:
    """(code, words) for what keeps an always-in house out anyway, or ("", "") when it is in. Read from
    the house's own record: a house in a higher lane or on a held list stays in that lane's work (a
    live lead, first to market, an obituary), and every reason the gate gives against assigning it
    holds it out except the two that close only one channel (ALWAYS_SOFT). Rank, the buy box, the
    discount-room floor and the people-only rule are never asked."""
    if not rec or rec.get("_gone") or not rec.get("uuid"):
        return "unreadable", gate.TEXT["unreadable"]
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    mine = lanes.lane_of(rec, cfg)
    if lanes.rank(mine) < lanes.rank(TRACK):
        return ("lead", "a live lead") if mine == "lead" else ("lane", "already in the %s lane" % lanes.name(mine))
    held = on_held_list(rec.get("lists"), held_lists)
    if held:
        return "lane", "on the %s list" % held
    v = ctx.verdict(rec, "assign", lane=TRACK)
    for code, detail in v.reasons:
        if code not in ALWAYS_SOFT:
            return code, gate.words(code, detail)
    return "", ""


def always_house(ctx, build, u: str, s: dict | None, rec: dict, p: dict, d4d: dict) -> dict:
    """An always-in house in the shape the score reads, for the ranking file: from its SiftMap row when
    the month's sweep has one, else from its own record (an always-in house can sit outside the buy
    box). Its tags, lists and status are the record's own."""
    if s is None:
        a = rec.get("address") or {}
        s = {"v": rec.get("estimate_value"), "e": rec.get("equity_percent"),
             "addr": (a.get("street") if isinstance(a, dict) else "") or p.get("street") or ""}
    r = house(u, s, _titles(rec.get("tags")), _titles(rec.get("lists")), lanes.status_of(rec), d4d)
    build.score(r)
    model = _ODDS["model"]
    if model is not None:
        import deal_odds
        facts = deal_odds.with_bands(deal_odds.facts_from_search_row(s), p.get("ten"), p.get("yb"))
        r["facts"] = deal_odds.facts_from_record(rec, facts, today=ctx.now.date())
        r["odds"] = live_odds(deal_odds.prob(model, r["facts"]), s, r)
    r["holds"], r["city"], r["always"] = bool(p.get("holds")), p.get("city") or "", True
    return r


def always_in(ctx, build, o: dict, sift: dict, pool: dict, owners: dict, dropped: dict, recs: dict, d4d: dict,
              out_by: Counter) -> tuple[dict, dict]:
    """({owner key: {houses, score, reach, always}} for every owner an always-in house puts in whatever
    their rank, {owner key: why} for every always-in owner held out whole). Each always-in house is
    judged on its own record in `recs` by always_hold: a hold about the person (OWNER_LEVEL), on that
    house or seen on another (`dropped`, or the ranking's own read of the owner), leaves the owner out
    whole; one about the house leaves out that house. The owner's other houses are the ones the
    ranking's rules kept, so the owner sits in one lane."""
    by_owner: dict = {}
    for u, p in pool.items():
        if p.get("always"):
            by_owner.setdefault(p.get("owner") or u, []).append(u)
    forced, held_whole = {}, {}
    for key in sorted(by_owner):
        us = sorted(by_owner[key])
        own = owners.get(key) or {}
        whole = (("%s on another house" % dropped[key]) if key in dropped else "") or own.get("whole") or ""
        passed = []
        if not whole and o.get("always_people_only") and (
                own.get("entity") or any(is_entity((recs.get(u) or {}).get("owner") or {}) for u in us if recs.get(u))):
            whole = "a company or trust, not a person"
        if not whole:
            for u in us:
                code, why = always_hold(ctx, recs.get(u) or {"uuid": u, "_gone": True}, o["exclude_lists"])
                if code in OWNER_LEVEL:
                    whole = why
                    break
                if code:
                    out_by["always in, but held out: %s" % why] += 1
                    continue
                passed.append(u)
        if whole:
            held_whole[key] = whole
            out_by["always in, but the owner is held out whole: %s" % whole] += len(us)
            continue
        if not passed:
            continue
        houses = list(own.get("kept", own.get("houses") or []))
        houses += [always_house(ctx, build, u, sift.get(u), recs[u], pool[u], d4d) for u in passed]
        pts, words = reach_of(build, recs[passed[0]])
        forced[key] = {"houses": houses, "score": owner_score(build, houses, pts, words), "reach": words,
                       "always": sorted({w for u in passed for w in pool[u].get("always") or []})}
    return forced, held_whole


def ranked(owners: dict, field: str = "pre") -> list[str]:
    return sorted(owners, key=lambda k: (-owners[k][field]["score"], -owners[k][field]["room"], k))


# ---------------------------------------------------------------------- the finalists' records
PRUNE_KEEP = ("uuid", "dataflik_id", "status", "mls", "do_not_mail_ever", "predictivecall_attempts", "sms_attempts",
              "directmail_attempts", "estimate_value", "equity_percent", "year", "bedrooms", "bathrooms", "sqft",
              "structure_type", "owned_since", "last_sold")
OWNER_KEEP = ("uuid", "first_name", "last_name", "company", "deceased", "opt_out", "dnc", "skiptraced", "do_not_mail_ever",
              "emails", "type")


def prune(rec: dict) -> dict:
    """A record kept to what the gate, the score and the agency's call list read. A whole record is
    several kilobytes; a month's finalists would otherwise fill the autopilot's own table."""
    from agents.autopilot.crm import titles
    out = {k: rec.get(k) for k in PRUNE_KEEP if k in rec}
    out["tags"] = titles(rec.get("tags"))
    out["lists"] = titles(rec.get("lists"))
    a = rec.get("address") or {}
    out["address"] = {k: a.get(k) for k in ("street", "city", "state", "postal_code", "county", "vacant")} if isinstance(a, dict) else {}
    ow = rec.get("owner") or {}
    o = {k: ow.get(k) for k in OWNER_KEEP if k in ow}
    m = ow.get("address") or {}
    o["address"] = {k: m.get(k) for k in ("street", "city", "state", "postal_code")} if isinstance(m, dict) else {}
    o["phones"] = [{"number": p.get("number"), "status": p.get("status"), "type": p.get("type"),
                    "tags": titles(p.get("tags")), "is_connected": p.get("is_connected")} for p in ow.get("phones") or []]
    out["owner"] = o
    sec = rec.get("secondary_owners") or []
    if sec and isinstance(sec[0], dict):
        out["secondary_owners"] = [{"first_name": sec[0].get("first_name"), "last_name": sec[0].get("last_name")}]
    return out


class Reader:
    """Full records, remembered for `days` (a fact per record), at most `cap` opened a run. A run that
    stops on the hour's request budget keeps what it read. A record that is gone answers _gone."""

    def __init__(self, ctx, cap: int, days: float):
        self.ctx, self.cap, self.days = ctx, int(cap), float(days)
        self.opened = self.cached = 0
        self.stopped = ""

    def fresh(self, f) -> bool:
        """A remembered read is used while it is younger than `days` on the run's own clock (the fact's
        own stamp is the wall clock, which a replayed or simulated run does not share)."""
        if self.days <= 0 or not isinstance(f, dict) or not f.get("uuid"):
            return False
        try:
            age = self.ctx.now - datetime.fromisoformat(str(f.get("_read_at") or ""))
        except (TypeError, ValueError):
            return False
        return timedelta(0) <= age <= timedelta(days=self.days)

    def get(self, uuid: str) -> dict | None:
        f = ledger.fact(uuid, FACT_RECORD) if self.days > 0 else None
        if self.fresh(f):
            self.cached += 1
            return f
        if self.stopped or self.opened >= self.cap:
            self.stopped = self.stopped or "cap"
            return None
        try:
            rec = self.ctx.crm.record(uuid)
        except budget.BudgetExhausted as exc:
            self.stopped = str(exc)
            return None
        except Exception as exc:  # noqa: BLE001  (a record that cannot be read is left out and said)
            if getattr(exc, "code", None) != 404:
                self.ctx.out.problem("could not read a record (%s)" % str(exc)[:100])
            return {"uuid": uuid, "_gone": True}
        self.opened += 1
        if not rec or not rec.get("uuid"):
            return {"uuid": uuid, "_gone": True}
        slim = dict(prune(rec), _read_at=self.ctx.now.isoformat(timespec="seconds"))
        ledger.set_fact(uuid, FACT_RECORD, slim)     # knowledge, kept in plan mode too
        return slim


def reread_note(ctx, rd: Reader, n_left: int) -> None:
    if rd.stopped == "cap":
        ctx.out.line("Read %d record(s) this run (the cap is %d); %d still to read, the next run continues." % (
            rd.opened, rd.cap, n_left))
    elif rd.stopped:
        ctx.out.problem("stopped reading early: %s; %d record(s) still to read, the next run continues" % (rd.stopped, n_left))


# ---------------------------------------------------------------------- the stage
def round_end(ctx) -> int:
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    return int((cfg.timeline(TRACK) or {}).get("round_ends_at", 5))


def in_round(ctx, rec: dict) -> bool:
    """The record's opening texts or call round have started and the round is not over."""
    calls, texts = int(rec.get("predictivecall_attempts") or 0), int(rec.get("sms_attempts") or 0)
    return (calls >= 1 or texts >= 1) and calls < round_end(ctx)


PULLED_RX = re.compile(r"^pulled_(\d{4}-\d{2}-\d{2})$")


def include_state_path(stage_id: str) -> Path:
    return tracks_dir() / ("%s-include.json" % stage_id)


def include_sources(ctx, o: dict, base: dict) -> list[tuple[str, dict]]:
    """(words, search filter) for every county source the daily pass reads: the monthly stage's
    always_include plus this stage's own, then each recent list's new arrivals (a pulled_<date> tag
    from the last recent_days). A title the account does not hold is a problem, never a quiet day."""
    always = always_include(base)
    own = always_include(o)
    for k in ALWAYS_KINDS:
        always[k] = sorted(set(always[k]) | set(own[k]))
    out = []
    for kind in ALWAYS_KINDS:
        for t in always[kind]:
            uid = ctx.crm.list_uuid(t) if kind == "lists" else ctx.crm.tag_uuid(t)
            if not uid:
                ctx.out.problem("always_include names the %s %s, which the account does not have; nobody was put in "
                                "for it" % ("list" if kind == "lists" else "tag", t))
                continue
            out.append((always_words(kind, t), {"any_lists" if kind == "lists" else "any_tags": [uid]}))
    recent = [str(t).strip() for t in (o.get("recent_lists") or []) if str(t).strip()]
    if recent:
        cutoff = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date() - timedelta(days=int(o["recent_days"]))
        pulled = sorted(u for u, title in (index(ctx.crm, "tag") or {}).items()
                        if PULLED_RX.match(title) and datetime.strptime(title[7:], "%Y-%m-%d").date() >= cutoff)
        for t in recent:
            uid = ctx.crm.list_uuid(t)
            if not uid:
                ctx.out.problem("recent_lists names the list %s, which the account does not have" % t)
                continue
            if not pulled:
                ctx.out.line("No record was pulled in the last %d days (no pulled_<date> tag), so nothing new came in "
                             "on the %s list." % (int(o["recent_days"]), t))
                continue
            out.append(("new on the %s list" % t, {"any_lists": [uid], "any_tags": pulled}))
    return out


def owner_elsewhere(ctx, owner_uuid: str, held_lists) -> str:
    """Why the owner is held on ANOTHER house ("" when nothing holds them), from three counts on the
    owner's records: a tag or list that puts a house in a higher lane or stops all contact (Do Not
    Market, a litigator, a possible death, first to market, an heir, an obituary, the DNC list), and a
    live lead status. The month's ranking learns this from its own pool; the daily pass asks."""
    if not owner_uuid:
        return ""
    # Most owners hold one house: one count settles it before the three that look at the others.
    if ctx.crm.count({"owners": [owner_uuid]}) <= 1:
        return ""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lcfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    tags = set(cfg.do_not_market_tags) | set(cfg.litigator_tags) | set(cfg.deceased_tags) | set(cfg.heir_tags)
    lists = set(cfg.suppress_lists) | set(held_lists or ())
    for lane in lanes.ORDER:
        if lanes.rank(lane) >= lanes.rank(TRACK):
            break
        m = (getattr(lcfg, "markers", None) or {}).get(lane) or {}
        tags |= set(m.get("tags") or [])
        lists |= set(m.get("lists") or [])
    tag_ids = [x for x in (ctx.crm.tag_uuid(t) for t in sorted(tags)) if x]
    list_ids = [x for x in (ctx.crm.list_uuid(t) for t in sorted(lists)) if x]
    if tag_ids and ctx.crm.count({"owners": [owner_uuid], "any_tags": tag_ids}):
        return "held on another house (a contact stop, a possible death or a higher lane's tag)"
    if list_ids and ctx.crm.count({"owners": [owner_uuid], "any_lists": list_ids}):
        return "held on another house (a list of a higher lane or the DNC list)"
    if ctx.crm.count({"owners": [owner_uuid], "any_property_status": list(lanes.LEAD_STATUS_VALUES)}):
        return "a live lead on another house"
    return ""


def run_include(ctx: Ctx) -> None:
    """The county feeds, daily (COUNTY FEEDS, DAILY above): every always-in house that does not hold
    the tag yet, judged with the month's hard holds, tagged in a live run. Only additions: the
    monthly stage keeps removing, so the two never fight over a house."""
    st = ctx.stage
    o = options(st)
    src = ctx.policy.stages.get(str(o.get("include_from") or "")) if o.get("include_from") else None
    base = options(src) if src is not None else o
    if o.get("include_from") and src is None:
        ctx.out.problem("include_from names the stage %s, which the playbook does not have; this stage's own "
                        "always_include is all it reads" % o["include_from"])
    tag = str(base["tag"]).strip()
    people_only = bool(base.get("always_people_only") or o.get("always_people_only"))
    tag_uid = ctx.crm.tag_uuid(tag)
    if not tag_uid:
        ctx.out.problem("the tag %s does not exist in the account; nobody was put in" % tag)
        return
    sources = include_sources(ctx, o, base)
    if not sources:
        ctx.out.head = "No county list to read (always_include is empty)."
        return
    spath = include_state_path(st.id)
    state = load_json(spath, {}) or {}
    held: dict = state.get("held") or {}
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    recheck = timedelta(days=int(o["include_recheck_days"]))
    from agents.autopilot.lanecheck import scan
    seen, owner_held, reads, adds, left = set(), {}, 0, [], 0
    by_source = Counter()
    for words, flt in sources:
        must = dict(flt, must_not={"any_tags": [tag_uid]})
        for r in scan(ctx, must):
            u = r.get("uuid")
            if not u or u in seen:
                continue
            seen.add(u)
            h = held.get(u)
            if h and today - datetime.strptime(h["at"], "%Y-%m-%d").date() < recheck:
                ctx.out.tally["left out on an earlier run: %s (read again every %d days)" % (
                    h["why"], int(o["include_recheck_days"]))] += 1
                continue
            if reads >= int(o["include_reads"]):
                left += 1
                continue
            reads += 1
            try:
                rec = ctx.crm.record(u)
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (a record that cannot be read is skipped and said)
                if getattr(exc, "code", None) == 404:
                    held[u] = {"why": "the record is gone", "at": today.isoformat()}
                    ctx.out.tally["left out: the record is gone (404)"] += 1
                else:
                    ctx.out.tally["left out: the record could not be read"] += 1
                continue
            ow = rec.get("owner") or {}
            okey = str(ow.get("uuid") or u)
            why = owner_held.get(okey, "")
            if not why and people_only and is_entity(ow):
                why = "a company or trust, not a person"
                owner_held[okey] = why
            code = "owner" if why else ""
            if not code:
                code, why = always_hold(ctx, rec, base["exclude_lists"])
                if code in OWNER_LEVEL:
                    owner_held[okey] = why
            if not code and ow.get("uuid"):
                why = owner_elsewhere(ctx, str(ow["uuid"]), base["exclude_lists"])
                if why:
                    code = "owner"
                    owner_held[okey] = why
            if code:
                held[u] = {"why": why, "at": today.isoformat()}
                ctx.out.tally["left out: %s" % why] += 1
                continue
            held.pop(u, None)
            adds.append((u, addr(rec), words))
            by_source[words] += 1
    written, stopped = 0, ""
    verified = bool(state.get("verified"))
    for u, street, words in adds:
        try:
            ctx.change(u, "add_tags", [], [tag])
            ctx.write("tag", ctx.crm.add_tags, u, [tag], {"tags": []})
        except budget.BudgetExhausted as exc:
            stopped = str(exc)
            break
        except Exception as exc:  # noqa: BLE001  (a failed write is said; what landed is kept)
            stopped = "tagging %s failed (%s)" % (street or u[:8], str(exc)[:80])
            break
        written += 1
        if ctx.live:
            if not verified:
                back = {t.strip().lower() for t in _titles(ctx.crm.record(u).get("tags"))}
                if tag.lower() not in back:
                    ctx.out.problem("the tag did not stick on %s; nothing more was written" % (street or u[:8]))
                    stopped = "read back"
                    break
                verified = True
            ledger.event(ctx.run_id, st.id, u, "top1000_add", {"tag": tag, "why": words}, now=ctx.now, tz=ctx.policy.tz)
    if ctx.live:
        save_json(spath, {"held": held, "verified": verified, "updated": ctx.now.isoformat(timespec="seconds")})
    else:
        # Knowledge, kept in plan mode too: what is held, so a plan run does not re-read it daily either.
        save_json(spath, dict(state, held=held, updated=ctx.now.isoformat(timespec="seconds")))
    ctx.out.acted = written if ctx.live else 0
    ctx.out.found = len(seen)
    verb = "Put" if ctx.live else "Would put"
    if adds:
        ctx.out.head = "%s the %s tag on %d county record(s) for the Top 1,000 texts: %s." % (
            verb, tag, written if ctx.live else len(adds),
            ", ".join("%d %s" % (n, w) for w, n in by_source.most_common()))
        for u, street, words in adds[:6]:
            ctx.out.line("%s (%s)" % (street or u[:8], words))
    else:
        ctx.out.head = "No new county record to put in the Top 1,000 (%d read, %d held)." % (reads, len(seen) - reads)
    if left:
        ctx.out.line("%d more wait for the next run (include_reads is %d)." % (left, int(o["include_reads"])))
    if stopped and stopped != "read back":
        ctx.out.problem("stopped after %d tag(s): %s; the next run continues" % (written, stopped))


def run_top1000(ctx: Ctx) -> None:
    st = ctx.stage
    if str(st.get("job") or "").strip().lower() == "include":
        run_include(ctx)
        return
    o = options(st)
    tag = str(o["tag"]).strip()
    month = month_of(ctx)
    mode = "live" if ctx.live else "plan"
    spath = state_path(st.id, month, mode)
    state = load_json(spath, {}) or {"month": month, "mode": mode, "started": ctx.now.isoformat(timespec="seconds")}
    if state.get("phase") == "done":
        ctx.out.head = "Nothing to do: this month's Top 1,000 was settled %s. %s" % (
            str(state.get("done") or "")[:10], (state.get("summary") or {}).get("line") or "")
        return
    if state.get("broken"):
        ctx.out.problem("the last live write did not stick (%s); nothing more is written until a person looks and "
                        "deletes %s" % (state["broken"], spath.name))
        return
    sift = month_sweep(ctx, o)
    if sift is None:
        return
    pool = cached_pool(ctx, o, spath.with_name(spath.stem + "-pool.json"), list(st.presets),
                       [tag] + [str(t) for t in o["pool_tags"]], [str(t) for t in o["pool_lists"]], holder_tag=tag,
                       always=always_include(o))
    if pool is None:
        return
    for kind in ("tag", "list"):
        if index(ctx.crm, kind) is None:
            ctx.out.problem("the account's %s index could not be read, so the houses' %ss are unknown; nothing was ranked" % (kind, kind))
            return
    try:
        build = scoring(o)
    except RuntimeError as exc:
        ctx.out.problem(str(exc))
        return
    try:
        _work(ctx, o, build, tag, sift, pool, state, spath)
    finally:
        restore(build)


def _work(ctx, o, build, tag, sift, pool, state, spath) -> None:
    d4d = d4d_scores(o)
    from agents.autopilot import pacing
    if not pacing.lane_map_built(ctx.policy):
        # An owner first to market on a house outside the pool is known only from that map; without
        # it the gate holds such a house later, after it has taken a slot here.
        ctx.out.line("The nightly owner-lane map has not been built, so an owner first to market on a house outside "
                     "the pool is not known here; the gate holds that house once the map exists.")
    always = {u for u, p in pool.items() if p.get("always")}
    dropped: dict = {}
    owners, out_by = candidates(ctx, build, sift, pool, TRACK, d4d, held_lists=o["exclude_lists"], always=always,
                                dropped_out=dropped)
    tag_id = ctx.crm.tag_uuid(tag) if tag else ""
    holders = {u for u, p in pool.items() if p.get("holds") or (tag_id and tag_id in (sift.get(u) or {}).get("tags", []))}
    order = ranked(owners)
    finalists = order[: int(math.ceil(o["size"] * float(o["finalists"])))]
    always_keys = {(pool[u].get("owner") or u) for u in always}
    ctx.out.found = len(set(owners) | always_keys)
    rd = Reader(ctx, o["max_reads"], o["facts_days"])
    left = 0
    # The always-in houses are read first, so the ranking's reads can never starve them. An owner a
    # hold about the person already leaves out whole needs no read.
    need = [u for u in sorted(always) if (pool[u].get("owner") or u) not in dropped]
    always_recs: dict = {}
    for u in need:
        rec = rd.get(u)
        if rec is None:
            left += 1
        else:
            always_recs[u] = rec
    for key in finalists:
        rec = rd.get(owners[key]["pre"]["best"])
        if rec is None:
            left += 1
        else:
            owners[key]["rec"] = rec
    if left:
        total = len(finalists) + len(need)
        reread_note(ctx, rd, left)
        save_json(spath, dict(state, phase="read", updated=ctx.now.isoformat(timespec="seconds"),
                              progress={"finalists": total, "read": total - left}))
        ctx.out.head = "Reading the finalists for this month's Top 1,000: %d of %d read." % (total - left, total)
        return
    # The final score: the full record's gate, then the owner's reach from their own phones.
    final: dict = {}
    for key in finalists:
        own = owners[key]
        rec = own.get("rec") or {}
        if rec.get("_gone"):
            own["kept"] = [h for h in own["houses"] if h["uuid"] != rec.get("uuid")]
            out_by["the record could not be read"] += 1
            continue
        v = ctx.verdict(rec, "assign", lane=TRACK)
        houses = list(own["houses"])
        entity = bool(_RULES["individuals_only"] and is_entity(rec.get("owner") or {}))
        if entity and key not in always_keys:
            # The full record is the last word: a search row with a person's name can hide a company
            # field or a trust in the name.
            out_by["owner left out whole: a company or trust, not a person"] += len(houses)
            continue
        if entity:
            own["entity"] = True     # an always-in owner is in whatever they are; ranked, the rule holds (below)
        held = on_held_list(rec.get("lists"), o["exclude_lists"])
        if held:
            own["whole"] = "on the %s list" % held
            out_by["owner left out whole: on the %s list" % held] += len(houses)
            continue
        if not v.ok:
            if v.code in OWNER_LEVEL:
                own["whole"] = v.reason()
                out_by["owner left out whole: %s" % v.reason()] += len(houses)
                continue
            houses = [h for h in houses if h["uuid"] != rec.get("uuid")]
            own["kept"] = houses
            out_by[v.reason()] += 1
            if not houses:
                continue
        model = _ODDS["model"]
        if model is not None:
            # The full record knows the exact purchase date and year built, where the search knew only bands.
            import deal_odds
            for h in houses:
                if h["uuid"] == rec.get("uuid") and h.get("facts") is not None:
                    h["facts"] = deal_odds.facts_from_record(rec, h["facts"], today=ctx.now.date())
                    h["odds"] = live_odds(deal_odds.prob(model, h["facts"]), sift.get(h["uuid"]), h)
        pts, words = reach_of(build, rec)
        final[key] = {"houses": houses, "score": owner_score(build, houses, pts, words), "reach": words}
    # The always-in owners take their slots first; the ranking fills the rest.
    forced, held_whole = always_in(ctx, build, o, sift, pool, owners, dropped, always_recs, d4d, out_by)
    for k in [k for k in final if k in always_keys and k not in forced and (owners.get(k) or {}).get("entity")]:
        # Every always-in house of this owner was held, so the owner is only ranked, and the people rule holds.
        out_by["owner left out whole: a company or trust, not a person"] += len(final[k]["houses"])
        del final[k]
    by_score = lambda d: sorted(d, key=lambda k: (-d[k]["score"]["score"], -d[k]["score"]["room"], k))  # noqa: E731
    rest = int(o["size"]) if o.get("always_on_top") else max(0, int(o["size"]) - len(forced))
    regular = [k for k in by_score(final) if k not in forced and k not in held_whole][:rest]
    final.update(forced)
    picked = by_score(forced) + regular
    tag_set = {h["uuid"] for k in picked for h in final[k]["houses"]}
    kept, leads, reads_left = set(), set(), 0
    dropping = sorted(holders - tag_set)
    # A holder that is a live lead now keeps the tag and is never read or written: a prospecting
    # stage does not touch a lead, and the tag is where the lead came from.
    leads = {u for u in dropping if lanes.status_of(pool.get(u) or {}) in lanes.LEAD_STATUSES}
    if o["keep_in_round"] and dropping:
        for u in dropping:
            if u in leads:
                continue
            rec = rd.get(u)
            if rec is None:
                reads_left += 1
                continue
            if rec.get("_gone"):
                continue
            if lanes.status_of(rec) in lanes.LEAD_STATUSES:
                leads.add(u)
            elif ctx.verdict(rec, "assign", lane=TRACK).ok and in_round(ctx, rec):
                kept.add(u)
        if reads_left:
            reread_note(ctx, rd, reads_left)
            save_json(spath, dict(state, phase="read", updated=ctx.now.isoformat(timespec="seconds")))
            ctx.out.head = "Reading the holders who fell out of this month's Top 1,000: %d still to read." % reads_left
            return
    adds = sorted(tag_set - holders)
    removes = sorted(holders - tag_set - kept - leads)
    streets = {u: p.get("street") or "" for u, p in pool.items()}
    missing_cond = sum(1 for k in picked if "cond" in final[k]["score"]["missing"])
    houses_n = len(tag_set)
    fd = files_dir(ctx)
    csv_path = fd / ("top1000_%s_%s.csv" % (state["month"], "live" if ctx.live else "plan"))
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(_csv_rows(picked, final, holders, kept, tag_set, streets, leads))
    where = link(ctx, csv_path)
    for why, n in out_by.most_common():
        ctx.out.tally["left out: %s" % why] += n
    ctx.out.tally["owners with a house still in"] = len(owners)
    ctx.out.tally["finalists read in full"] = len(finalists)
    if len(picked) < o["size"]:
        ctx.out.line("Only %d owners passed every check, short of %s (raise finalists to read more of the ranking)." % (
            len(picked), format(o["size"], ",")))
    model = _ODDS["model"]
    if model is not None:
        t = (model.get("test") or {}).get("capture") or [{}]
        ctx.out.line("Ranked on deal odds (fitted %s; tested on the half-year after its training window, its top 1,000 "
                     "houses held %s investor deals, %.1f times the base rate). Expected investor sales in six months "
                     "across the chosen owners: %.0f." % (
                         model.get("fitted") or "?", t[0].get("deals", "?"), float(t[0].get("lift") or 0),
                         sum(final[k]["score"].get("odds", 0.0) for k in picked)))
    elif missing_cond:
        ctx.out.line("%d of the %d owners were scored without a condition score (no D4D visual score): their other "
                     "components were scaled to 100." % (missing_cond, len(picked)))
    if forced:
        said = Counter(w for k in forced for w in forced[k]["always"])
        ctx.out.line("%d owner(s) are in whatever their rank (always_include: %s); the ranking filled the other %d slot(s)." % (
            len(forced), ", ".join("%d %s" % (n, w) for w, n in sorted(said.items(), key=lambda kv: (-kv[1], kv[0]))),
            len(regular)))
    held_always = sum(n for w, n in out_by.items() if w.startswith("always in, but"))
    if held_always:
        ctx.out.line("%d always-in house(s) were held out anyway by a hard hold (a lead, first to market, sold, a finished "
                     "status, Do Not Market and the like; each is counted below)." % held_always)
    for i, k in enumerate(regular[:3], 1):
        sc = final[k]["score"]
        if model is not None:
            ctx.out.line("rank %d: %s, %.1f%% in six months, %d house(s), $%s discount room, %s; %s" % (
                i, streets.get(sc["best"]) or sc["best"][:8], 100.0 * sc.get("odds", 0.0), len(final[k]["houses"]),
                format(int(sc["room"]), ","), final[k]["reach"], ", ".join(sc.get("why") or []) or "no single strong signal"))
            continue
        ctx.out.line("rank %d: %s, score %.1f, %d house(s), $%s discount room, %s" % (
            i, streets.get(sc["best"]) or sc["best"][:8], sc["score"], len(final[k]["houses"]),
            format(int(sc["room"]), ","), final[k]["reach"]))
    if kept:
        ctx.out.line("%d holder house(s) fell out of the top %s and keep the tag while their round finishes." % (
            len(kept), format(o["size"], ",")))
    if leads:
        ctx.out.line("%d holder house(s) fell out because they are live leads now; they keep the tag and are not touched." % len(leads))
    ctx.out.line("The ranking: %s" % where)
    summary = {"owners": len(picked), "houses": houses_n, "adds": len(adds), "removes": len(removes), "kept": len(kept),
               "leads": len(leads), "always": len(forced), "file": str(csv_path), "link": where}
    keep_state = dict(state, owners=sorted(picked), tagged=sorted(tag_set | kept | leads), always=sorted(forced))
    if not ctx.live:
        ctx.out.acted = len(adds) + len(removes)
        line = ("Top 1,000 for %s (plan): %d owners on %d houses%s; %d house(s) would get the tag and %d would lose it."
                % (state["month"], len(picked), houses_n, keep_words(summary), len(adds), len(removes)))
        ctx.out.head = ("Would put the %s tag on %d owners (%d houses)%s: %d house(s) would get it and %d would lose it. "
                        "Nothing is written in plan mode." % (tag, len(picked), houses_n, keep_words(summary), len(adds), len(removes)))
        save_json(spath, dict(keep_state, phase="done", done=ctx.now.isoformat(timespec="seconds"), summary=dict(summary, line=line)))
        return
    apply_changes(ctx, o, tag, adds, removes, keep_state, spath, summary, streets)


def keep_words(summary: dict) -> str:
    """', and 2 house(s) from last month keep it (1 mid-round, 1 a live lead)', or nothing."""
    parts = [(summary.get("kept") or 0, "mid-round"), (summary.get("leads") or 0, "a live lead" if summary.get("leads") == 1 else "live leads")]
    n = sum(k for k, _ in parts)
    if not n:
        return ""
    return ", and %d house(s) from last month keep it (%s)" % (n, ", ".join("%d %s" % (k, w) for k, w in parts if k))


def apply_changes(ctx, o, tag, adds, removes, state, spath, summary, streets) -> None:
    """Add and remove the tag, at most max_writes a run, each change recorded with what it replaced.
    The first add and the first remove are read back before any other write of that kind."""
    done = state.get("applied") or {"added": [], "removed": []}
    have = {"add": set(done["added"]), "remove": set(done["removed"])}
    # A record that answered 404 while being tagged is gone (merged or deleted since the sweep): it is
    # skipped and remembered, never retried. Found live 2026-11-13: one such record stopped the first
    # run at 1,417 of 1,526, and every later run would have stopped on it again.
    gone = set(state.get("gone") or [])

    def keep(phase: str, **more) -> None:
        save_json(spath, dict(state, phase=phase, applied={"added": sorted(have["add"]), "removed": sorted(have["remove"])},
                              gone=sorted(gone), updated=ctx.now.isoformat(timespec="seconds"), **more))

    writes, stopped = 0, ""
    for kind, todo in (("add", adds), ("remove", removes)):
        for u in todo:
            if u in have[kind] or u in gone:
                continue
            if writes >= int(o["max_writes"]):
                stopped = "cap"
                break
            try:
                if kind == "add":
                    ctx.change(u, "add_tags", [], [tag])
                    ctx.write("tag", ctx.crm.add_tags, u, [tag], {"tags": []})
                else:
                    ctx.change(u, "remove_tags", [tag], [])
                    ctx.write("tag", ctx.crm.remove_tags, u, [tag])
            except budget.BudgetExhausted as exc:
                stopped = str(exc)
                break
            except Exception as exc:  # noqa: BLE001  (a failed write is said, and what landed is kept)
                if getattr(exc, "code", None) == 404:
                    gone.add(u)
                    ctx.out.tally["left out: the record is gone (404 when tagged)"] += 1
                    continue
                stopped = "the %s of the tag on %s failed (%s)" % (kind, streets.get(u) or u[:8], str(exc)[:80])
                break
            writes += 1
            have[kind].add(u)
            if not state.get("verified_%s" % kind):
                back = {t.strip().lower() for t in _titles(ctx.crm.record(u).get("tags"))}
                if (tag.lower() in back) != (kind == "add"):
                    state["broken"] = "the %s of the tag on %s read back wrong" % (kind, streets.get(u) or u[:8])
                    ctx.out.problem("the tag did not %s on %s; nothing more was written" % (
                        "stick" if kind == "add" else "come off", streets.get(u) or u[:8]))
                    keep("apply")
                    return
                state["verified_%s" % kind] = True
            ledger.event(ctx.run_id, ctx.stage.id, u, "top1000_%s" % kind, {"tag": tag}, now=ctx.now, tz=ctx.policy.tz)
            if writes % 50 == 0:
                keep("apply")
        if stopped:
            break
    ctx.out.acted = writes
    if stopped:
        left = (len([u for u in adds if u not in have["add"] and u not in gone])
                + len([u for u in removes if u not in have["remove"] and u not in gone]))
        if stopped == "cap":
            ctx.out.line("Wrote %d tag change(s) this run (the cap is %d); %d still to write, the next run continues." % (
                writes, int(o["max_writes"]), left))
        else:
            ctx.out.problem("stopped writing early: %s; %d tag change(s) still to write, the next run continues" % (stopped, left))
        keep("apply")
        ctx.out.head = "Writing this month's Top 1,000: %d of %d tag change(s) done." % (
            len(have["add"]) + len(have["remove"]), len(adds) + len(removes))
        return
    line = ("Top 1,000 for %s: %d owners on %d houses hold the tag%s; %d house(s) got it and %d lost it."
            % (state["month"], summary["owners"], summary["houses"], keep_words(summary), len(have["add"]), len(have["remove"])))
    ctx.out.head = ("The %s tag now holds %d owners (%d houses)%s: %d house(s) got it and %d lost it (run %s; "
                    "cli.py autopilot undo %s --commit puts it back)." % (
                        tag, summary["owners"], summary["houses"], keep_words(summary), len(have["add"]), len(have["remove"]),
                        ctx.run_id, ctx.run_id))
    keep("done", done=ctx.now.isoformat(timespec="seconds"), summary=dict(summary, line=line))


def _titles(items) -> list[str]:
    from agents.autopilot.crm import titles
    return titles(items)


CSV_HEAD = ["Rank", "Score", "Houses", "Best house", "City", "Discount room", "Deal points", "Condition points",
            "Investor points", "Evidence points", "Reach points", "Reach", "Scored without", "Holds the tag now",
            "Decision", "Record id"]


CSV_HEAD_ODDS = ["Rank", "Odds in six months (%)", "Houses", "Best house", "City", "Discount room", "Why",
                 "Reach", "Reach factor", "Holds the tag now", "Decision", "Record id"]


def _csv_rows(picked, final, holders, kept, tag_set, streets, leads=frozenset()) -> list[list]:
    odds = any("odds" in final[k]["score"] for k in picked)
    rows = [CSV_HEAD_ODDS if odds else CSV_HEAD]
    for i, k in enumerate(picked, 1):
        f = final[k]
        sc = f["score"]
        best = next((h for h in f["houses"] if h["uuid"] == sc["best"]), f["houses"][0])
        holds = any(h["uuid"] in holders for h in f["houses"])
        decision = "keep" if holds else "add"
        if f.get("always"):
            decision += ": always in, %s" % "; ".join(f["always"])
        if odds:
            rows.append([i, round(100.0 * float(sc.get("odds") or 0.0), 2), len(f["houses"]), best.get("street") or "",
                         best.get("city") or "", int(sc["room"]), "; ".join(sc.get("why") or []), f["reach"],
                         sc.get("reach_factor", ""), "yes" if holds else "", decision, best["uuid"]])
            continue
        rows.append([i, sc["score"], len(f["houses"]), best.get("street") or "", best.get("city") or "", int(sc["room"]),
                     sc["pts"].get("deal", ""), sc["pts"].get("cond", ""), sc["pts"].get("inv", ""), sc["pts"].get("evid", ""),
                     sc["pts"].get("reach", ""), f["reach"], ", ".join(sc["missing"]), "yes" if holds else "",
                     decision, best["uuid"]])
    for u in sorted(holders - tag_set):
        why = ("kept while its round finishes" if u in kept else "kept: a live lead, not touched" if u in leads else "remove")
        # rank, score, houses, street, then blanks up to the last three columns: holds, decision, record id
        rows.append(["", "", "", streets.get(u) or ""] + [""] * (len(rows[0]) - 7) + ["yes", why, u])
    return rows


# ---------------------------------------------------------------------- the morning digest
def digest_lines(policy, now: datetime) -> list[str]:
    """One line per monthly track stage that finished in the last three days, from its own month state."""
    out = []
    month = now.astimezone(ZoneInfo(policy.tz)).strftime("%Y-%m")
    for sid, st in (getattr(policy, "stages", None) or {}).items():
        if st.mode == "off" or st.kind not in ("top1000", "agencyexport"):
            continue
        s = month_state(sid, month)
        line = (s.get("summary") or {}).get("line")
        if not line or not s.get("done"):
            continue
        try:
            if now - datetime.fromisoformat(s["done"]) > timedelta(days=3):
                continue
        except ValueError:
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------- the operator's command line
def may_write(commit: bool) -> bool:
    """The operator's apply writes only with both switches: --commit on the line and TOP1000_ALLOW=1
    in the environment, so a pasted command cannot tag a thousand houses by itself."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The Top 1,000: plan (read-only) or apply the tag on purpose.")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--stage", default="top1000_refresh", help="the playbook stage whose settings to use")
        p.add_argument("--again", action="store_true", help="forget this month's finished %s and work it out again" % name)
        if name == "apply":
            p.add_argument("--commit", action="store_true", help="write the tag (also needs %s=1)" % ALLOW_ENV)
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw or raw[0] not in ("plan", "apply", "-h", "--help"):
        raw = ["plan"] + raw
    args = ap.parse_args(raw)
    import uuid as _uuid
    from agents.autopilot import policy as policy_mod
    from agents.autopilot.crm import CRM
    store.init()
    pol = policy_mod.load()
    if pol is None:
        print("this install has no team/autopilot.yaml")
        return 1
    st = pol.stage(args.stage)
    live = args.cmd == "apply" and may_write(getattr(args, "commit", False))
    if args.cmd == "apply" and not live:
        print("DRY RUN: apply writes only with --commit and %s=1; this run plans and writes nothing." % ALLOW_ENV)
    now = datetime.now(timezone.utc)
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=live)
    run_id = "top1000-" + _uuid.uuid4().hex[:8]
    ctx = Ctx(policy=pol, stage=st, crm=CRM(), run_id=run_id, now=now, live=live, out=out)
    if args.again:
        p = state_path(st.id, month_of(ctx), "live" if live else "plan")
        s = load_json(p, {})
        if s.get("phase") == "done":
            p.unlink()
            print("forgot this month's finished %s run (%s)" % ("live" if live else "plan", p.name))
    try:
        run_top1000(ctx)
    except budget.BudgetExhausted as exc:
        out.problem("stopped early: %s" % exc)
    print(out.head)
    for ln in out.lines:
        print("  " + ln)
    for p in out.problems:
        print("  problem: " + p)
    for k, v in sorted(out.tally.items(), key=lambda kv: -kv[1])[:20]:
        print("  %6d  %s" % (v, k))
    if live and out.acted:
        print("run %s: python src/agents/cli.py autopilot undo %s --commit puts every change back" % (run_id, run_id))
    return 1 if out.problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
