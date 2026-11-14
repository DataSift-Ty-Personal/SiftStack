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
from agents.autopilot.stages import Ctx, Summary  # noqa: E402

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
}
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
    if not isinstance(counties, list) or not counties:
        out.append("stage %s: name the counties to search, as counties: [{fips, name, state}]" % st.id)
    elif any(not isinstance(c, dict) or not (c.get("fips") and c.get("name") and c.get("state")) for c in counties):
        out.append("stage %s: each county needs fips, name and state" % st.id)
    if "tag" in (st.params or {}) and not str(st.get("tag") or "").strip():
        out.append("stage %s: tag may not be empty" % st.id)
    for key in ("exclude_lists", "pool_lists", "pool_tags"):
        v = st.get(key)
        if v is not None and not isinstance(v, list):
            out.append("stage %s: %s must be a list of titles" % (st.id, key))
    w = str(st.get("weights") or "today").strip().lower()
    if w not in ("today", "taught"):
        out.append("stage %s: weights must be today or taught (the Dream 100 weight sets)" % st.id)
    return out


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
    """The few fields of a SiftMap search row the score and the exclusions read."""
    d = row.get("distressors") or {}
    flags = sorted(k for k, v in d.items() if (v.get("is_active") if isinstance(v, dict) else v))
    pre = row.get("presets") or {}
    return {"v": row.get("estimatedValue"), "e": row.get("equityPercent"), "i": row.get("investor_off_market_score"),
            "r": row.get("realtor_score"), "abs": bool(row.get("absenteeOwner")), "corp": bool(row.get("corporateOwned")),
            "use": row.get("propertyUse") or "", "mls": mls_word(row), "tags": list(row.get("saved_tags") or []),
            "lists": list(row.get("saved_lists") or []), "flags": flags, "vac": bool(pre.get("vacant")),
            "addr": row.get("address") or "", "fips": str(row.get("fips") or "")}


class _Asked:
    def __init__(self, cap: int):
        self.cap, self.n = int(cap), 0


def _sweep_slice(ctx, asked: _Asked, county: dict, filters: dict, lo: int, hi: int, page: int, out: dict,
                 stats: Counter) -> bool:
    """Every row in one county and one value band, bisected until a band holds fewer than SPLIT_AT
    rows. False when the run's search allowance ran out first."""
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
                return (_sweep_slice(ctx, asked, county, filters, lo, mid, page, out, stats)
                        and _sweep_slice(ctx, asked, county, filters, mid + 1, hi, page, out, stats))
            if total > SEARCH_WALL:
                stats["rows past the search wall, not read"] += total - SEARCH_WALL
        rows = r.get("data") or []
        for row in rows:
            u = row.get("saved_uuid")
            if not (row.get("saved") or u) or not u:
                stats["rows not in the account"] += 1
                continue
            out[u] = slim_row(row)
        got += len(rows)
        offset += len(rows)        # the next page starts after the rows read, whatever size a page is
        if not rows or got >= min(total, SEARCH_WALL):
            stats["rows read"] += got
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
                      "box": BUY_BOX, "exclude": sorted(exclude)}, sort_keys=True)
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
def pool_rows(ctx, o: dict, presets: list[str], tag_titles=(), list_titles=(), holder_tag: str = "") -> dict | None:
    """record uuid -> {owner, status, street, city, holds} from the stage's pool presets, the records
    carrying the pool tags and the records on the pool lists. A search row is the only place the
    owner's id is, and a page of 250 costs one request. None when a source is larger than the
    block's max_pool allows (the run says which)."""
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
        sources.append(("preset %s" % title, must, False))
    for t in tag_titles:
        uid = ctx.crm.tag_uuid(t)
        if uid:
            sources.append(("tag %s" % t, {"any_tags": [uid]}, bool(holder_tag) and t.strip().lower() == holder_tag.strip().lower()))
        elif holder_tag and t.strip().lower() == holder_tag.strip().lower():
            ctx.out.tally["the tag %s does not exist yet: nobody holds it" % t] += 1
    for t in list_titles:
        uid = ctx.crm.list_uuid(t)
        if uid:
            sources.append(("list %s" % t, {"any_lists": [uid]}, False))
    for label, must, holds in sources:
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
                                  "src": label, "from": []}
            have["holds"] = bool(have["holds"] or holds)
            if label not in have["from"]:
                have["from"].append(label)
            out[u] = have
        ctx.out.tally["pool rows from the %s" % label] = len(rows)
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


def cached_pool(ctx, o: dict, path: Path, presets, tag_titles=(), list_titles=(), holder_tag: str = "") -> dict | None:
    """The pool, read once and kept for `pool_days`: a run that continues reading the next night
    must not page the account again to learn the same owners."""
    have = load_json(path, {})
    try:
        fresh = ctx.now - datetime.fromisoformat(have.get("at") or "") < timedelta(days=float(o["pool_days"]))
    except (TypeError, ValueError):
        fresh = False
    if fresh and isinstance(have.get("rows"), dict):
        ctx.out.tally["pool rows (kept from %s)" % str(have["at"])[:10]] = len(have["rows"])
        return have["rows"]
    rows = pool_rows(ctx, o, presets, tag_titles, list_titles, holder_tag)
    if rows is not None:
        save_json(path, {"at": ctx.now.isoformat(timespec="seconds"), "rows": rows})
    return rows


# ---------------------------------------------------------------------- the score (Dream 100's)
def scoring(o: dict):
    """dream100_build with the block's weight set on it. Imported here, not at the top: it carries
    the workbook code and the D4D client, which a playbook check should not have to load."""
    import dream100
    import dream100_build as build
    dream100.apply_weights(str(o.get("weights") or "today"), build)
    return build


def restore(build) -> None:
    import dream100
    dream100.apply_weights(dream100.DEFAULT_WEIGHTS, build)


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


def owner_score(build, houses: list[dict], reach_pts: float | None = None) -> dict:
    """The owner's score: their best house's Dream 100 score, with deal size counted over every house
    they hold in the buy box. A component the data does not have (condition with no D4D score,
    reach before the record is read) is left out and the rest are scaled back to 100."""
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
               held_lists=()) -> tuple[dict, Counter]:
    """owner key -> {"houses": [...], "pre": owner_score without reach} for every owner with a house
    still in, and the houses left out, counted by reason. `skip(uuid, owner)` returns a reason to
    leave a house out before it is scored (the agency's 90-day rule), or ''. A house on a held list
    (exclude_lists) leaves its whole owner out."""
    out_by: Counter = Counter()
    owners: dict = {}
    dropped: dict = {}
    for u, p in pool.items():
        s = sift.get(u)
        if s is None:
            out_by["no SiftMap row in the buy box (outside the counties, the value or single family, or tagged out)"] += 1
            continue
        key = p.get("owner") or u
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
        r["holds"] = bool(p.get("holds"))
        r["city"] = p.get("city") or ""
        owners.setdefault(key, {"houses": []})["houses"].append(r)
    for key, why in dropped.items():
        if key in owners:
            out_by["owner left out whole: %s on another house" % why] += len(owners[key]["houses"])
            del owners[key]
    for o in owners.values():
        o["pre"] = owner_score(build, o["houses"])
    return owners, out_by


def ranked(owners: dict, field: str = "pre") -> list[str]:
    return sorted(owners, key=lambda k: (-owners[k][field]["score"], -owners[k][field]["room"], k))


# ---------------------------------------------------------------------- the finalists' records
PRUNE_KEEP = ("uuid", "dataflik_id", "status", "mls", "do_not_mail_ever", "predictivecall_attempts", "sms_attempts",
              "directmail_attempts", "estimate_value", "equity_percent", "year", "bedrooms", "bathrooms", "sqft",
              "structure_type")
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


def run_top1000(ctx: Ctx) -> None:
    st = ctx.stage
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
                       [tag] + [str(t) for t in o["pool_tags"]], [str(t) for t in o["pool_lists"]], holder_tag=tag)
    if pool is None:
        return
    for kind in ("tag", "list"):
        if index(ctx.crm, kind) is None:
            ctx.out.problem("the account's %s index could not be read, so the houses' %ss are unknown; nothing was ranked" % (kind, kind))
            return
    build = scoring(o)
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
    owners, out_by = candidates(ctx, build, sift, pool, TRACK, d4d, held_lists=o["exclude_lists"])
    tag_id = ctx.crm.tag_uuid(tag) if tag else ""
    holders = {u for u, p in pool.items() if p.get("holds") or (tag_id and tag_id in (sift.get(u) or {}).get("tags", []))}
    order = ranked(owners)
    finalists = order[: int(math.ceil(o["size"] * float(o["finalists"])))]
    ctx.out.found = len(owners)
    rd = Reader(ctx, o["max_reads"], o["facts_days"])
    left = 0
    for key in finalists:
        rec = rd.get(owners[key]["pre"]["best"])
        if rec is None:
            left += 1
        else:
            owners[key]["rec"] = rec
    if left:
        reread_note(ctx, rd, left)
        save_json(spath, dict(state, phase="read", updated=ctx.now.isoformat(timespec="seconds"),
                              progress={"finalists": len(finalists), "read": len(finalists) - left}))
        ctx.out.head = "Reading the finalists for this month's Top 1,000: %d of %d read." % (len(finalists) - left, len(finalists))
        return
    # The final score: the full record's gate, then the owner's reach from their own phones.
    final: dict = {}
    for key in finalists:
        own = owners[key]
        rec = own.get("rec") or {}
        if rec.get("_gone"):
            out_by["the record could not be read"] += 1
            continue
        v = ctx.verdict(rec, "assign", lane=TRACK)
        houses = list(own["houses"])
        held = on_held_list(rec.get("lists"), o["exclude_lists"])
        if held:
            out_by["owner left out whole: on the %s list" % held] += len(houses)
            continue
        if not v.ok:
            if v.code in OWNER_LEVEL:
                out_by["owner left out whole: %s" % v.reason()] += len(houses)
                continue
            houses = [h for h in houses if h["uuid"] != rec.get("uuid")]
            out_by[v.reason()] += 1
            if not houses:
                continue
        pts, words = reach_of(build, rec)
        final[key] = {"houses": houses, "score": owner_score(build, houses, pts), "reach": words}
    picked = sorted(final, key=lambda k: (-final[k]["score"]["score"], -final[k]["score"]["room"], k))[: o["size"]]
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
    if missing_cond:
        ctx.out.line("%d of the %d owners were scored without a condition score (no D4D visual score): their other "
                     "components were scaled to 100." % (missing_cond, len(picked)))
    for i, k in enumerate(picked[:3], 1):
        sc = final[k]["score"]
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
               "leads": len(leads), "file": str(csv_path), "link": where}
    keep_state = dict(state, owners=sorted(picked), tagged=sorted(tag_set | kept | leads))
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


def _csv_rows(picked, final, holders, kept, tag_set, streets, leads=frozenset()) -> list[list]:
    rows = [CSV_HEAD]
    for i, k in enumerate(picked, 1):
        f = final[k]
        sc = f["score"]
        best = next((h for h in f["houses"] if h["uuid"] == sc["best"]), f["houses"][0])
        holds = any(h["uuid"] in holders for h in f["houses"])
        rows.append([i, sc["score"], len(f["houses"]), best.get("street") or "", best.get("city") or "", int(sc["room"]),
                     sc["pts"].get("deal", ""), sc["pts"].get("cond", ""), sc["pts"].get("inv", ""), sc["pts"].get("evid", ""),
                     sc["pts"].get("reach", ""), f["reach"], ", ".join(sc["missing"]), "yes" if holds else "",
                     "keep" if holds else "add", best["uuid"]])
    for u in sorted(holders - tag_set):
        why = ("kept while its round finishes" if u in kept else "kept: a live lead, not touched" if u in leads else "remove")
        rows.append(["", "", "", streets.get(u) or "", "", "", "", "", "", "", "", "", "", "yes", why, u])
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
