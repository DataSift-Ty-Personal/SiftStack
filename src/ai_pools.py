"""Ranked, buy-boxed, non-overlapping record pools for a calling agency, built on the install's own Open API key.

The pools are chosen from SiftMap's search (the published route, read in volume as acceptable use allows);
nothing is added to the account here. Pull a pool in afterwards and build the agency's file from it:

  python src/ai_pools.py --county "Knox, TN" --county "Blount, TN" --pool investor:30000 --name knox_blount
  python src/ai_pools.py --county "Franklin, OH" --pool realtor:20000 --pool investor:20000 --rank ladder
  python src/pull_by_ids.py --ids output/ai_pools/<name>/<pool>.json --list "<list name>"     # the quote; --commit pulls
  python src/agency_call_list.py --pool output/ai_pools/<name>/<pool>.json --list "<list name>" --phase all

Rankings
  investor --rank ai      SiftMap's investor AI score, one point at a time from 100 down. Search rows do not
                          carry the score, so each score is its own filter (investor_off_market_score_min and
                          _max, whole numbers, proven by count).
  investor --rank ladder  the county's Doors per Deal ladder (learn.datasift.ai/capture-data/<st>.json) in work
                          order, each rung its own SiftMap filter (the same translation live Doors per Deal
                          uses), preset rungs (equity and occupancy cuts) skipped, then the investor AI order
                          for the rest. Two counties merge on marginal doors per deal, each keeping its own
                          rung order.
  realtor                 SiftMap's realtor AI score, one point at a time from 100 down.
  Inside one score or rung, more equity first, then the SiftMap id.

The AI scores are a per-county add-on: a county whose scores this account cannot see is said so at the start. Its
investor pool falls back to the ladder's list rungs; a realtor pool has no fallback there.

Buy box: single family; the value band (--value); not listed (extra_mls_status not_listed); no sale since --since
(each band's houses that sold since are searched and taken out, so a house with no sale on record stays in); no
company owner (each row's own corporateOwned); --exclude-zips; the ids in --exclude. A trust or a nameless owner
is not visible before a pull: agency_call_list.py holds those out of the call file. A neighborhood is not on a
search row, so a neighborhood cut is a ZIP cut here.

No overlap: a house in two pools goes to the pool where it ranks higher and the other pool refills from its
next-best house, until neither holds the other's houses.

Output: output/ai_pools/<name>/<pool>.json (ordered rows carrying the score or rung that put each house there)
and summary.json. Searches are paced at about 80 a minute (SiftMap's limit is per account, shared by every tool
on it) and capped by --max-searches; the summary counts them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dpd_live as D  # noqa: E402

OUT = Path(os.environ.get("AI_POOLS_DIR") or "output/ai_pools")
INVESTOR = "investor_off_market_score"
REALTOR = "realtor_score"
SCORE_KEYS = {"investor": INVESTOR, "realtor": REALTOR}
BOX = {"type_single_family": True, "extra_mls_status": ["not_listed"]}
PAGE = 250
SPLIT_AT = 9000           # past 10,000 rows SiftMap refuses a page, so a bigger result is cut by value first
ZIP_RX = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


# ------------------------------------------------------------------------------------------ the search
def members(s, c: dict, filters: dict, lo: int, hi: int, out: dict | None = None, cut: Counter | None = None) -> dict:
    """id -> row for every house in one county matching `filters` with a value from lo to hi. A result bigger
    than SPLIT_AT is cut in two by value until each part reads in full under the 10,000-row wall."""
    out = {} if out is None else out
    f = dict(filters, value_min=int(lo), value_max=int(hi))
    offset, total = 0, None
    while True:
        r = s(D.body(c, f, offset, PAGE))
        if total is None:
            total = int(r.get("total_results") or 0)
            if total > SPLIT_AT and hi - lo >= 2:
                mid = (int(lo) + int(hi)) // 2
                members(s, c, filters, lo, mid, out, cut)
                members(s, c, filters, mid + 1, hi, out, cut)
                return out
            if total > D.WALL and cut is not None:
                cut["over 10,000 houses at one value, only 10,000 read"] += total - D.WALL
        rows = r.get("data") or []
        for row in rows:
            if row.get("id") is not None:
                out[str(row["id"])] = row
        offset += len(rows)
        if not rows or offset >= min(total, D.WALL):
            return out


def row_zip(row: dict) -> str:
    m = ZIP_RX.search(str(row.get("address") or ""))
    return m.group(1) if m else ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yes(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


class Market:
    """The counties, the buy box and the cuts, with every house it drops counted by reason."""

    def __init__(self, s, counties: list[dict], value: tuple[int, int], since: str, exclude_zips=(), skip=()):
        self.s, self.counties, self.value, self.since = s, counties, (int(value[0]), int(value[1])), since or ""
        self.exclude_zips, self.skip = set(exclude_zips), {str(i) for i in skip}
        self.dropped: Counter = Counter()
        self._counted: set[str] = set()     # a house on several ladder lists is counted once

    def count(self, c: dict, extra: dict | None = None) -> int:
        return self.s.count(c, dict(BOX, value_min=self.value[0], value_max=self.value[1], **(extra or {})))

    def take(self, c: dict, extra: dict) -> list[dict]:
        """The houses in one county matching the buy box and `extra`, after the cuts, more equity first."""
        got = members(self.s, c, dict(BOX, **extra), *self.value, cut=self.dropped)
        sold = {}
        if got and self.since:
            sold = members(self.s, c, dict(BOX, extra_last_sale_date_min=self.since, **extra), *self.value)
        rows = []
        for i, r in got.items():
            why = ("on the exclude list" if i in self.skip else
                   "sold since %s" % self.since if i in sold else
                   "company owner" if _yes(r.get("corporateOwned")) else
                   "excluded ZIP" if row_zip(r) in self.exclude_zips else "")
            if why:
                if i not in self._counted:
                    self._counted.add(i)
                    self.dropped[why] += 1
            else:
                rows.append(r)
        rows.sort(key=lambda r: (-(_num(r.get("equityPercent")) or 0), str(r.get("id"))))
        return rows


def slim(row: dict, c: dict, **more) -> dict:
    i = str(row.get("id"))
    return dict({"dataflik_id": int(i) if i.isdigit() else i, "fips": c["fips"], "property_zip": row_zip(row),
                 "equity_percentage_est": _num(row.get("equityPercent")), "property_value": _num(row.get("estimatedValue")),
                 "address": row.get("address")}, **more)


def sees_scores(m: Market, c: dict, key: str) -> bool:
    """True when this account sees the AI score in this county. Without the add-on the filter matches nothing;
    a filter the search ignored would answer the whole county even at a perfect score."""
    any_score = m.count(c, {key + "_min": 1})
    return any_score > 0 and not (any_score == m.count(c) and m.count(c, {key + "_min": 100}) == any_score)


# ------------------------------------------------------------------------------------------ rankings
def by_score(m: Market, pool: str, need: int, taken: set, counties: list[dict] | None = None, out=print) -> list[dict]:
    """One point of the pool's AI score at a time, from 100 down, every county at that score together."""
    key = SCORE_KEYS[pool]
    counties = m.counties if counties is None else counties
    rows: list[dict] = []
    for score in range(100, 0, -1):
        if len(rows) >= need or not counties:
            break
        band = [slim(r, c, rung="%s AI %d" % (pool.title(), score), priority=None, **{key: score})
                for c in counties for r in m.take(c, {key + "_min": score, key + "_max": score})
                if str(r["id"]) not in taken]
        band.sort(key=lambda x: (-(x["equity_percentage_est"] or 0), str(x["dataflik_id"])))
        added = 0
        for x in band:
            if len(rows) >= need:
                break
            taken.add(str(x["dataflik_id"]))
            rows.append(x)
            added += 1
        if band:
            out("  %s AI %3d: %6d in the box, %6d taken, total %6d" % (pool, score, len(band), added, len(rows)))
    return rows


def ladder(c: dict, variant: str | None, fetch=None) -> list[dict]:
    """The county's published ladder as SiftMap filters, in work order, preset rungs marked to skip."""
    cap = D.shard("capture", c["fips"][:2], fetch).get(c["fips"])
    if not cap:
        raise SystemExit("the Doors per Deal ladder has no county %s (%s); use --rank ai" % (c["name"], c["fips"]))
    _, rungs = D.ladder_rungs(cap, variant)
    out = []
    for r in rungs:
        x = {"county": c, "label": r["label"], "priority": r["priority"], "dpd": r["shard"]["marg_dpd"],
             "list_size": r["shard"]["size"], "filters": r["filters"]}
        if r["ai"] is None and any(str(k).startswith("preset_") for k in r["keys"]):
            x["skip"] = "preset rung (a pool is for equity)"
        out.append(x)
    return out


def merge_ladders(ladders: list[list[dict]]) -> list[dict]:
    """Merge on marginal doors per deal, each county keeping its own rung order."""
    idx, out = [0] * len(ladders), []
    while True:
        best = None
        for li, lad in enumerate(ladders):
            if idx[li] < len(lad):
                d = lad[idx[li]]["dpd"] if lad[idx[li]]["dpd"] is not None else float("inf")
                if best is None or d < best[0]:
                    best = (d, li)
        if best is None:
            return out
        out.append(ladders[best[1]][idx[best[1]]])
        idx[best[1]] += 1


def by_ladder(m: Market, seq: list[dict], need: int, taken: set, fill_counties: list[dict], out=print) -> tuple[list, list]:
    rows: list[dict] = []
    per: list[dict] = []
    for r in seq:
        if len(rows) >= need:
            break
        c = r["county"]
        if "skip" in r:
            per.append({"fips": c["fips"], "label": r["label"], "priority": r["priority"], "skipped": r["skip"]})
            continue
        got, added = m.take(c, r["filters"]), 0
        for x in got:
            if len(rows) >= need:
                break
            if str(x["id"]) in taken:
                continue
            taken.add(str(x["id"]))
            rows.append(slim(x, c, rung=r["label"], priority=r["priority"]))
            added += 1
        per.append({"fips": c["fips"], "label": r["label"], "priority": r["priority"], "dpd": r["dpd"],
                    "in_box": len(got), "added": added})
        out("  %s P%s %-46s in box %6d  added %6d  total %6d" % (c["fips"], r["priority"], r["label"][:46],
                                                              len(got), added, len(rows)))
    if len(rows) < need and fill_counties:
        fill = by_score(m, "investor", need - len(rows), taken, fill_counties, out=out)
        for x in fill:
            x["rung"] = "Fill: investor AI %s" % x.get(INVESTOR)
        rows += fill
        per.append({"label": "Fill: investor AI", "added": len(fill)})
    return rows, per


def disjoint(a: list, b: list, na: int, nb: int) -> tuple:
    """A house in both pools goes where it ranks higher (ties to a); the other pool refills. Only the houses each
    pool will actually keep (its first na or nb) can collide."""
    pa = {x["dataflik_id"]: i for i, x in enumerate(a)}
    pb = {x["dataflik_id"]: i for i, x in enumerate(b)}
    owner: dict = {}
    for _ in range(100):
        fa = [x for x in a if owner.get(x["dataflik_id"]) != "b"][:na]
        fb = [x for x in b if owner.get(x["dataflik_id"]) != "a"][:nb]
        both = {x["dataflik_id"] for x in fa} & {x["dataflik_id"] for x in fb}
        if not both:
            return fa, fb, Counter(owner.values())
        for i in both:
            owner[i] = "a" if pa[i] <= pb[i] else "b"
    raise SystemExit("GATE FAILED: overlap resolution did not converge")


# ------------------------------------------------------------------------------------------ inputs
def id_set(path: str) -> set[str]:
    """Ids from a JSON list of rows (dataflik_id / id), a list of ids, or a dict keyed by id."""
    if not path:
        return set()
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit("could not read the id file %s" % path)
    if isinstance(d, dict) and isinstance(d.get("claimed"), dict):
        return {str(k) for k in d["claimed"]}
    if isinstance(d, dict) and not any(isinstance(v, list) for v in d.values()):
        return {str(k) for k in d}
    rows = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
    return {str(r.get("dataflik_id") or r.get("id")) if isinstance(r, dict) else str(r) for r in rows}


def describe(rows: list, prior: set, pool: str) -> dict:
    key = SCORE_KEYS.get(pool, INVESTOR)
    sc = [x[key] for x in rows if x.get(key) is not None]
    return {"records": len(rows),
            "by_priority": dict(Counter("P%s" % x["priority"] if x.get("priority") else str(x["rung"]).split(" AI ")[0]
                                        for x in rows)),
            "top_rungs": Counter(x["rung"] for x in rows).most_common(12),
            "score_range": [min(sc), max(sc)] if sc else None,
            "by_county": dict(Counter(x["fips"] for x in rows)),
            "already_on_prior_list": sum(1 for x in rows if str(x["dataflik_id"]) in prior)}


def parse_pools(specs: list[str]) -> list[tuple[str, int]]:
    out = []
    for sp in specs:
        name, _, n = str(sp).partition(":")
        name = name.strip().lower()
        if name not in SCORE_KEYS or not n.strip().isdigit() or int(n) < 1:
            raise SystemExit('a pool is "investor:<count>" or "realtor:<count>" (got %r)' % sp)
        out.append((name, int(n)))
    if not out or len(out) > 2 or len({p for p, _ in out}) != len(out):
        raise SystemExit("name one or two pools: --pool investor:30000 [--pool realtor:20000]")
    return out


def parse_value(v: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*\$?([\d,]+)\s*-\s*\$?([\d,]+)\s*", str(v or ""))
    if not m:
        raise SystemExit("--value is a band like 100000-700000 (got %r)" % v)
    lo, hi = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    if hi <= lo:
        raise SystemExit("--value: the top must be above the bottom")
    return lo, hi


def resolve_counties(specs: list[str]) -> list[dict]:
    from curative_engine.counties import resolve
    out = []
    for sp in specs:
        c = resolve(sp)
        if not c.fips or not c.state:
            raise SystemExit('no US county matches %r; name it with its state, like "Knox, TN", or give its FIPS code' % sp)
        if c.fips not in {x["fips"] for x in out}:
            out.append({"fips": c.fips, "name": c.name, "state": c.state})
    return out


# ------------------------------------------------------------------------------------------ the run
def build(s, counties: list[dict], pools: list[tuple[str, int]], *, rank: str = "ai", value=(1, 700000),
          since: str = "2023-01-01", exclude_zips=(), skip=(), margin: float = 1.5, shard_fetch=None, out=print) -> dict:
    """Choose the pools with the search `s` (a dpd_live.Search). Returns {"pools": {name: rows}, "summary": {...}}."""
    m = Market(s, counties, value, since, exclude_zips, skip)
    box = {c["fips"]: m.count(c) for c in counties}
    sees = {k: {c["fips"]: sees_scores(m, c, SCORE_KEYS[k]) for c in counties} for k, _ in pools}
    if rank == "ladder" and "investor" not in sees:
        sees["investor"] = {c["fips"]: sees_scores(m, c, INVESTOR) for c in counties}
    blind = {k: [c["name"] for c in counties if not v[c["fips"]]] for k, v in sees.items()}
    for k, names in blind.items():
        if names:
            out("NOTE: this account sees no %s AI scores in %s (a per-county add-on)%s" % (
                k, ", ".join(names), "; the ladder's list rungs carry it" if k == "investor" and rank == "ladder" else ""))
    two = len(pools) == 2
    picked, per_rung, taken = {}, {}, {}
    for name, n in pools:
        need = int(n * (margin if two else 1))
        taken[name] = set()
        scored = [c for c in counties if sees[name][c["fips"]]]
        if name == "investor" and rank == "ladder":
            # the ladder's own choice of variant, unless this account cannot see the scores its AI rungs need
            ladders = [ladder(c, None if sees["investor"][c["fips"]] else "lists", shard_fetch) for c in counties]
            seq = ladders[0] if len(ladders) == 1 else merge_ladders(ladders)
            picked[name], per_rung[name] = by_ladder(m, seq, need, taken[name], scored, out=out)
        else:
            if not scored:
                raise SystemExit("this account sees no %s AI scores in %s, so a %s pool cannot be ranked there%s" % (
                    name, ", ".join(c["name"] for c in counties), name,
                    " (try --rank ladder)" if name == "investor" else ""))
            picked[name] = by_score(m, name, need, taken[name], scored, out=out)
    split = None
    if two:
        (a, na), (b, nb) = pools
        picked[a], picked[b], moved = disjoint(picked[a], picked[b], na, nb)
        split = {"overlaps_kept_by_%s" % a: moved.get("a", 0), "overlaps_kept_by_%s" % b: moved.get("b", 0)}
        if {x["dataflik_id"] for x in picked[a]} & {x["dataflik_id"] for x in picked[b]}:
            raise SystemExit("GATE FAILED: the pools still overlap")
    short = {}
    for name, n in pools:
        rows = picked[name][:n]
        for i, x in enumerate(rows, 1):
            x["rank"] = i
        picked[name] = rows
        if len(rows) < n:
            short[name] = n - len(rows)
            out("WARNING: the %s pool holds %d of the %d asked for: the buy box ran out" % (name, len(rows), n))
    summary = {"when": datetime.now().isoformat(timespec="seconds"), "counties": counties, "rank": rank,
               "value": list(m.value), "since": m.since, "exclude_zips": sorted(m.exclude_zips), "excluded_ids": len(m.skip),
               "source": "SiftMap search on the account's own Open API key", "searches": s.n,
               "buy_box_houses": box, "dropped": dict(m.dropped), "overlap_split": split, "short": short,
               "ai_scores_visible": sees,
               "not_checked_before_the_pull": "trusts, nameless owners and neighborhoods: agency_call_list.py holds a "
                                              "trust, company or nameless owner of record out of the call file",
               "investor_rungs": per_rung.get("investor"), "pools": {}}
    return {"pools": picked, "summary": summary}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--county", action="append", required=True, help='a county, e.g. "Knox, TN" or 47093 (repeat for more)')
    ap.add_argument("--pool", action="append", required=True, help="investor:<count> or realtor:<count> (one or two)")
    ap.add_argument("--name", default="", help="the folder the pools go in (default: the counties)")
    ap.add_argument("--rank", default="ai", choices=["ai", "ladder"], help="the investor pool: AI score, or the Doors per Deal ladder")
    ap.add_argument("--value", default="1-700000", help="the value band, e.g. 100000-700000")
    ap.add_argument("--since", default="2023-01-01", help="leave out houses that sold on or after this day ('' keeps them)")
    ap.add_argument("--exclude-zips", default="", help="comma list of ZIP codes to leave out")
    ap.add_argument("--exclude", default="", help="ids never to pick (a JSON list, or a dict keyed by id); the pools refill past them")
    ap.add_argument("--prior", default="", help="a list already sent (ids JSON): the summary counts how many pool houses were on it")
    ap.add_argument("--margin", type=float, default=1.5, help="candidates per slot before the overlap split (two pools)")
    ap.add_argument("--max-searches", type=int, default=4000, help="stop the run past this many searches")
    args = ap.parse_args(argv)
    import datasift_api_upload as up
    key = (os.environ.get("REISIFT_API_KEY") or up.env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("REISIFT_API_KEY is not set: put your DataSift Open API key in .env at the repo root")
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        raise SystemExit("--since is a day like 2023-01-01")
    counties = resolve_counties(args.county)
    pools = parse_pools(args.pool)
    name = re.sub(r"[^a-z0-9]+", "_", (args.name or "_".join(c["name"] for c in counties)).lower()).strip("_")
    run = OUT / name
    run.mkdir(parents=True, exist_ok=True)
    s = D.Search(key=key, gap=0.75, cap=args.max_searches)
    try:
        res = build(s, counties, pools, rank=args.rank, value=parse_value(args.value), since=args.since,
                    exclude_zips={z.strip() for z in args.exclude_zips.split(",") if z.strip()},
                    skip=id_set(args.exclude), margin=args.margin)
    except D.SearchError as exc:
        raise SystemExit("stopped after %d searches: %s" % (s.n, exc))
    prior = id_set(args.prior)
    summary = res["summary"]
    summary.update(market=name, prior_list=args.prior or None)
    for p, rows in res["pools"].items():
        (run / ("%s.json" % p)).write_text(json.dumps(rows, indent=0), encoding="utf-8")
        summary["pools"][p] = describe(rows, prior, p)
    (run / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("buy_box_houses", "dropped", "overlap_split", "searches")}, default=str))
    for p, d in summary["pools"].items():
        print(p, json.dumps(d, default=str))
    first = run / ("%s.json" % pools[0][0])
    print('Next: python src/pull_by_ids.py --ids %s --list "<list name>"   (the quote; --commit pulls them in)' % first)
    return 0


if __name__ == "__main__":
    sys.exit(main())
