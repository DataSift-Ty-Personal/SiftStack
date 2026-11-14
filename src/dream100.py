"""Dream 100: the hundred largest opportunities in the D4D top 3,000.

Joins three things the account already holds for every D4D record:
  * the Driving for Dollars visual distress rank (output/d4d_call_list/cohort.json),
  * the record's own DataFlik scores (investor_score, realtor_score, on_market_score)
    plus every deal-size field on the CRM record (value, equity, mortgage, taxes,
    last sale, rental value, vacancy, age, deceased, dates),
  * the lists actually attached to the record (Tired Landlord, Vacant, Tax
    Delinquent, Pre-Foreclosure, Probate, ...), which are the account's own
    distress evidence,
and the phone tiers the D4D run already bought (phone_scores.json).

    python src/dream100.py --phase pull          # full CRM records (resumable)
    python src/dream100.py --phase build         # score, rank, write the workbook
    python src/dream100.py --phase attach [--commit]   # list + tag "Dream 100" on the top rows
    python src/dream100.py --phase compare       # both weight sets side by side, from the cached records

pull and build are read-only. attach is the ONLY write, DRY by default, and it
reads one record back before touching the rest.

Two named weight sets (build plan 5.2, decision D31, not decided yet):
  today   deal size 35, condition 20, investor 20, evidence 15, reach 10. The default.
  taught  a reachable number first, then deal size: reach 35, deal size 25, and the other three in
          today's proportions. The lesson: a big deal nobody can reach is not on the list.
`--weights taught` (or DREAM100_WEIGHTS=taught) chooses it. A build on a weight set that is not the
configured default writes into its own folder (output/dream100/<name>/), so trying one never
replaces the ranking that attach and the deep prospect read.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import d4d_call_list as d4d  # noqa: E402

OUT = Path("output/dream100")
FULL_PATH = OUT / "full_records.json"

# ------------------------------------------------------------ weights (decision D31) --
# The score itself lives in dream100_build (its W_* constants and TIER_PTS). These sets are applied
# onto that module for one run; "today" puts back exactly what the module shipped with, so the
# default can never drift from it. A test holds the "today" row to the module's own numbers.
WEIGHT_SETS = {
    "today": {"deal": 35, "cond": 20, "inv": 20, "evid": 15, "reach": 10},
    "taught": {"reach": 35, "deal": 25, "cond": 15, "inv": 15, "evid": 10},
}
DEFAULT_WEIGHTS = "today"
WEIGHTS_ENV = "DREAM100_WEIGHTS"
# The share of the reach points each dial tier earns. Today's 10, 7, 4 and 2 of 10 are these shares.
TIER_SHARE = {"Dial First": 1.0, "Dial Second": 0.7, "Dial Third": 0.4, "Dial Fourth": 0.2, "Drop": 0.0, "": 0.0}
_ATTR = {"deal": "W_DEAL", "cond": "W_COND", "inv": "W_INV", "evid": "W_EVID", "reach": "W_REACH"}
_LABEL = {"deal": "deal size", "cond": "condition", "inv": "investor", "evid": "evidence", "reach": "reach"}
_SHIPPED: dict = {}      # the build module's own numbers, remembered the first time they are touched


def weights_name(name: str | None = None) -> str:
    """The weight set to score with: the one asked for, else the environment's, else today's."""
    chosen = (name or os.environ.get(WEIGHTS_ENV) or DEFAULT_WEIGHTS).strip().lower()
    if chosen not in WEIGHT_SETS:
        raise ValueError("unknown weight set %r (use %s)" % (chosen, " or ".join(sorted(WEIGHT_SETS))))
    return chosen


def _build_module(build=None):
    if build is None:
        import dream100_build as build  # noqa: PLC0415  (openpyxl loads only when a build is asked for)
    if not _SHIPPED:
        _SHIPPED.update({a: getattr(build, a) for a in _ATTR.values()})
        _SHIPPED["TIER_PTS"] = dict(build.TIER_PTS)
        _SHIPPED["OUT"] = build.OUT
    return build


def apply_weights(name: str | None = None, build=None) -> dict:
    """Put a named weight set onto the scoring module and return it. "today" restores the module's
    own numbers exactly, whatever was applied before."""
    build = _build_module(build)
    chosen = weights_name(name)
    if chosen == DEFAULT_WEIGHTS:
        for attr in _ATTR.values():
            setattr(build, attr, _SHIPPED[attr])
        build.TIER_PTS = dict(_SHIPPED["TIER_PTS"])
        return dict(WEIGHT_SETS[chosen])
    w = WEIGHT_SETS[chosen]
    for key, attr in _ATTR.items():
        setattr(build, attr, w[key])
    build.TIER_PTS = {tier: round(w["reach"] * share, 1) for tier, share in TIER_SHARE.items()}
    return dict(w)


def out_dir_for(name: str | None = None) -> Path:
    """Where a build on this weight set writes. The configured default owns the main folder; any
    other set gets a folder of its own, so an experiment cannot overwrite the working ranking."""
    chosen = weights_name(name)
    return OUT if chosen == weights_name(None) else OUT / chosen


def _stated(w: dict) -> dict:
    """The sentences the workbook uses to state a weight set, keyed by what each one is."""
    tiers = [("%g" % round(w["reach"] * TIER_SHARE[t], 1)) for t in ("Dial First", "Dial Second", "Dial Third", "Dial Fourth")]
    out = {"subtitle": ", ".join("%s %d" % (_LABEL[k], w[k]) for k in ("deal", "cond", "inv", "evid", "reach")),
           "tiers": "Dial First %s, Second %s, Third %s, Fourth %s" % tuple(tiers)}
    for k in _ATTR:
        out["head:" + k] = "%s (%d)" % (_LABEL[k].capitalize(), w[k])
    return out


def restate_workbook(path: Path, name: str | None = None) -> list[str]:
    """Make a workbook built on another weight set say so in its own words.

    The build module writes "deal size 35 ... reach 10" as fixed text. Scored on other weights, that
    text would state numbers the ranking did not use, and the sheet is what a person reads. Each
    statement of today's weights is replaced by the set's own; anything that could not be found is
    returned, so the caller can say the sheet still states the old numbers instead of trusting it."""
    from openpyxl import load_workbook  # noqa: PLC0415

    chosen = weights_name(name)
    old, new = _stated(WEIGHT_SETS[DEFAULT_WEIGHTS]), _stated(WEIGHT_SETS[chosen])
    if old == new:
        return []
    wb = load_workbook(path)
    hit = set()
    text = []
    for ws in wb.worksheets[:2]:
        for row in ws.iter_rows():
            for c in row:
                v = c.value
                if not isinstance(v, str):
                    continue
                text.append(v)
                for key, was in old.items():
                    if key.startswith("head:"):
                        if v == was:
                            c.value = v = new[key]
                            hit.add(key)
                    elif was in v:
                        c.value = v = v.replace(was, new[key])
                        hit.add(key)
    missing = [key for key in old if key not in hit and not any(new[key] in t for t in text)]
    path = Path(path)
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    tmp.replace(path)
    return missing


def _load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=0, default=str), encoding="utf-8")
    tmp.replace(p)


def phase_pull(args) -> None:
    cohort = _load(d4d.COHORT_PATH, [])
    full = _load(FULL_PATH, {})
    todo = [r for r in cohort if r.get("saved_uuid") and (args.refresh or r["saved_uuid"] not in full)]
    print("pull: %d to read, %d cached" % (len(todo), len(full)), flush=True)
    api = d4d.Api()
    failed = 0
    t0 = time.time()
    for n, r in enumerate(todo, 1):
        u = r["saved_uuid"]
        try:
            rec = d4d._call(api, "/api/internal/property/%s/" % u)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  read failed %s: %s" % (u, str(e)[:120]), flush=True)
            continue
        rec["_fetched_at"] = datetime.now().isoformat(timespec="seconds")
        full[u] = rec
        if n % 100 == 0:
            _save(FULL_PATH, full)
            rate = n / max(time.time() - t0, 1)
            print("  %d/%d  (%.2f/s, %d failed)" % (n, len(todo), rate, failed), flush=True)
    _save(FULL_PATH, full)
    print("pull: done, %d records, %d failed" % (len(full), failed), flush=True)


ATTACH_LIST = "Dream 100"
ATTACH_TAG = "Dream 100"


def _ranked_with(name: str, full: dict, cohort: dict, scores: dict, build) -> list:
    """Every qualified record scored on one weight set, in the build phase's own order."""
    apply_weights(name, build)
    rows = []
    for u, rec in full.items():
        r = build.flatten(rec, cohort.get(u) or {}, scores)
        if not build.gate_reason(r):
            rows.append(build.score(r))
    return sorted(rows, key=lambda r: (-r["opp"], -r["deal_size"], r["d4d_rank"] or 9999))


def compare(full: dict, cohort: dict, scores: dict, top: int = 100, build=None) -> dict:
    """The top N under today's weights and under the taught ones, from the same records: how many
    rows are the same, how many of each top N can be reached, and what each is worth. This is the
    picture to decide D31 on. The scoring module is left on today's weights when it returns."""
    build = _build_module(build)
    try:
        sets = {name: _ranked_with(name, full, cohort, scores, build)[:top] for name in ("today", "taught")}
    finally:
        apply_weights(DEFAULT_WEIGHTS, build)
    out = {"top": top}
    for name, rows in sets.items():
        out[name] = {
            "rows": len(rows),
            "reachable": sum(1 for r in rows if r["best_tier"] in ("Dial First", "Dial Second") and not r["dnc"]),
            "no_number_or_dnc": sum(1 for r in rows if r["dnc"] or not r["best_tier"]),
            "discount_room": int(sum(r["deal_size"] for r in rows)),
            "uuids": [r["uuid"] for r in rows],
        }
    a, b = set(out["today"]["uuids"]), set(out["taught"]["uuids"])
    out["same"] = len(a & b)
    rank_today = {r["uuid"]: i for i, r in enumerate(sets["today"], 1)}
    rank_taught = {r["uuid"]: i for i, r in enumerate(sets["taught"], 1)}
    out["enter"] = [{"street": r["street"], "rank": rank_taught[r["uuid"]], "reach": r["reach"], "deal_size": r["deal_size"]}
                    for r in sets["taught"] if r["uuid"] not in a]
    out["leave"] = [{"street": r["street"], "rank": rank_today[r["uuid"]], "reach": r["reach"], "deal_size": r["deal_size"]}
                    for r in sets["today"] if r["uuid"] not in b]
    return out


def phase_compare(args) -> dict:
    full = _load(FULL_PATH, {})
    if not full:
        raise SystemExit("no full records; run --phase pull first")
    cohort = {}
    for r in _load(d4d.COHORT_PATH, []):
        u = r.get("saved_uuid")
        if u and (u not in cohort or (r.get("rank") or 9999) < (cohort[u].get("rank") or 9999)):
            cohort[u] = r
    scores = _load(d4d.SCORES_PATH, {})
    got = compare(full, cohort, scores, top=args.top)
    n = got["top"]
    for name in ("today", "taught"):
        s, w = got[name], WEIGHT_SETS[name]
        print("%-6s (%s): top %d holds %d reachable on a Dial First or Second number, %d with no number or on DNC, "
              "$%s of discount room" % (name, ", ".join("%s %d" % (_LABEL[k], w[k]) for k in w), s["rows"],
                                        s["reachable"], s["no_number_or_dnc"], format(s["discount_room"], ",")))
    print("%d of the top %d are the same under both; %d change" % (got["same"], n, len(got["enter"])))
    for label, rows in (("come in under taught", got["enter"]), ("drop out under taught", got["leave"])):
        for r in rows[:5]:
            print("  %s: %s (rank %d, %s, $%s room)" % (label, r["street"], r["rank"], r["reach"],
                                                      format(int(r["deal_size"]), ",")))
    _save(OUT / "weights_compare.json", {k: v for k, v in got.items()})
    return got


def phase_attach(args) -> None:
    """Attach the Dream 100 list + tag to the ranked top rows (accumulates)."""
    where = out_dir_for(getattr(args, "weights", None))
    ranked = _load(where / "dream100_ranked.json", [])
    if not ranked:
        raise SystemExit("no ranking in %s; run --phase build first" % where)
    top = ranked[: args.top]
    api = d4d.Api()
    for kind, title in (("list", ATTACH_LIST), ("tag", ATTACH_TAG)):
        d4d._ensure_title(api, kind, title, args.commit)
    if not args.commit:
        print("attach: DRY RUN, would attach list %r + tag %r to %d records; re-run with --commit" % (
            ATTACH_LIST, ATTACH_TAG, len(top)))
        return
    state = _load(OUT / "attach_state.json", {})
    body = {"lists": [ATTACH_LIST], "tags": [ATTACH_TAG]}
    # one first, read back, then the rest
    first = top[0]
    d4d.attach_to_record(api, first["uuid"], body)
    rec = d4d._call(api, "/api/internal/property/%s/" % first["uuid"])
    lists = [(l.get("title") if isinstance(l, dict) else l) for l in rec.get("lists") or []]
    tags = d4d.tag_names(rec)
    if ATTACH_LIST not in lists or ATTACH_TAG not in tags:
        raise SystemExit("read-back FAILED on %s: lists=%s tags=%s" % (first["uuid"], lists, tags))
    print("attach: read-back OK on rank 1 (%s)" % first["street"], flush=True)
    state[first["uuid"]] = "ok"
    done = 0
    for r in top[1:]:
        if state.get(r["uuid"]) == "ok":
            continue
        try:
            d4d.attach_to_record(api, r["uuid"], body)
            state[r["uuid"]] = "ok"
            done += 1
        except Exception as e:  # noqa: BLE001
            state[r["uuid"]] = "failed: %s" % str(e)[:120]
            print("  failed %s: %s" % (r["street"], str(e)[:120]), flush=True)
        if done % 20 == 0:
            _save(OUT / "attach_state.json", state)
    _save(OUT / "attach_state.json", state)
    ok = sum(1 for v in state.values() if v == "ok")
    print("attach: %d of %d records carry list %r + tag %r" % (ok, len(top), ATTACH_LIST, ATTACH_TAG))


def phase_build(args):
    """Score, rank and write the workbook on the chosen weight set (today's unless asked)."""
    import dream100_build as build  # noqa: PLC0415
    chosen = weights_name(getattr(args, "weights", None))
    _build_module(build)
    where = out_dir_for(chosen)
    try:
        w = apply_weights(chosen, build)
        build.OUT = where
        where.mkdir(parents=True, exist_ok=True)
        got = build.phase_build(args)
        if chosen != DEFAULT_WEIGHTS:
            book = sorted(where.glob("Dream100_*.xlsx"))[-1]
            missing = restate_workbook(book, chosen)
            print("weights: %s (%s)" % (chosen, ", ".join("%s %d" % (_LABEL[k], w[k]) for k in w)))
            if missing:
                print("WARNING: the workbook still states today's weights in %d place(s): %s" % (
                    len(missing), ", ".join(missing)))
            if where != OUT:
                print("written to %s; the ranking in %s was not touched" % (where, OUT))
        return got
    finally:
        build.OUT = _SHIPPED["OUT"]
        apply_weights(DEFAULT_WEIGHTS, build)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["pull", "build", "attach", "compare"], required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--weights", choices=sorted(WEIGHT_SETS), default=None,
                    help="the weight set to score with (default: today's, or DREAM100_WEIGHTS)")
    args = ap.parse_args(argv)
    if args.phase == "pull":
        phase_pull(args)
    elif args.phase == "attach":
        phase_attach(args)
    elif args.phase == "compare":
        phase_compare(args)
    else:
        phase_build(args)


if __name__ == "__main__":
    main()
