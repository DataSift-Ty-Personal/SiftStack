"""Franklin County OH (39049): the top 30,000 records by Doors per Deal, into ty+1.

Walks the County List Playbook's blended market share ladder
(learn.datasift.ai/capture-data/39.json, key 39049, variant "ai"): list rungs
and AI score rungs in the page's own Work Order, each rung counting only the
records the rungs above did not already reach. Every rung is re-sized under
Ty's filters, deduped, and the walk stops at 30,000 unique records.

Filters on every rung (Ty, 2026-09-29):
  * single family, buy box $1-700K, off market   (siftmap_pull.BUY_BOX)
  * any year built: the 1940 floor was lifted (Ty) once entities came out,
    because individuals-only 1940+ ran out of above-baseline records at ~16.5K
  * no sale on or after 2023-01-01 (clean equity; never-sold records stay in)
  * none of the Market Finder neighborhoods with zero investor activity or
    outlier pricing (phase neighborhoods)
  * no entity owners: extra_owner_type individual only (no LLC, corp, trust)
  * Negative Equity rungs are skipped: they contradict the equity goal

Why sizing runs on the ty+2 Open API key and ty+1 receives records BY ID:
ty+1's JWT carries no AI score addon, and SiftMap silently ignores a filter it
will not apply. An add-by-query on ty+1 carrying the AI filter could pull the
entire county buy box. Supply is account-independent, so ids are collected
under ty+2 and handed to ty+1 as ids.

    python src/franklin_top30k.py --phase probe
    python src/franklin_top30k.py --phase neighborhoods
    python src/franklin_top30k.py --phase count
    python src/franklin_top30k.py --phase sweep
    python src/franklin_top30k.py --phase pull            # dry
    python src/franklin_top30k.py --phase pull --commit
    python src/franklin_top30k.py --phase qa
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from siftmap_pull import BUY_BOX, WORLD, county_address  # noqa: E402
from siftmap_standalone import SiftMapClient  # noqa: E402

FIPS = "39049"
COUNTY_LABEL = "Franklin OH"
TARGET = 30000
PAGE = 250
SPLIT_AT = 9000        # keep every paged query under the 10,000-row offset wall
SOLD_CUTOFF_MIN = "2023-01-01"      # sold on/after this date = excluded
YEAR_MIN = None        # Ty 2026-09-29: pre-1940 allowed (was 1940); None = no floor

OUT = os.path.join(os.environ.get("SIFTSTACK_OUTPUT_DIR", "output"),
                   "franklin_top30k")
CAPTURE = os.path.join(OUT, "capture-data-39.json")
PROBE_FILE = os.path.join(OUT, "probe.json")


def _load_env() -> None:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _save(path: str, data) -> None:
    os.makedirs(OUT, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _load(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class _Bearer:
    """Duck-typed for siftmap_pull.Map: a pasted JWT with no way to re-mint."""

    def __init__(self, token: str):
        self.token = token

    def _mint(self) -> None:
        raise SystemExit("sizing bearer token was rejected (expired?): paste a "
                         "fresh one into the file SIFTMAP_BEARER_FILE points at")


class Search:
    """/properties/search/ for sizing and id collection.

    Default credential is the ty+2 Open API key. The off-market AI score is a
    PER-COUNTY add-on and ty+2 holds Knox + Blount only, so AI rungs need an
    account that bought the county: set SIFTMAP_BEARER_FILE to a file holding
    that account's JWT (2026-09-29: data+staging@dataflik.com, 3,144 counties).
    The token is read from a file so it never lands in a command line or repo.
    """

    def __init__(self):
        _load_env()
        self.calls = 0
        self.map = None
        self.sm = None
        self.who = "ty+2 Open API key"
        path = os.environ.get("SIFTMAP_BEARER_FILE", "").strip()
        if path:
            from siftmap_pull import Map
            tok = open(path, encoding="utf-8").read().strip()
            self.map = Map(_Bearer(tok))
            body = tok.split(".")[1]
            body += "=" * (-len(body) % 4)
            import base64
            self.who = json.loads(base64.urlsafe_b64decode(body)).get("email")
        else:
            self.sm = SiftMapClient(min_interval=0.25, max_retries=6)

    def page(self, filters: dict, address: dict, result_index: int = 1) -> dict:
        self.calls += 1
        body = {"result_index": result_index, "with_boundaries": False,
                "filters": filters, "addresses": [address], "polygon": WORLD}
        if self.map is None:
            return self.sm._request("/properties/search/", method="POST",
                                    body=body, timeout=120)
        # Map._post retries HTTP 429/5xx but not a dropped connection, and a
        # TLS reset mid-run (seen live) otherwise kills a 300-call sweep.
        import urllib.error
        for attempt in range(6):
            time.sleep(0.25)
            try:
                return self.map._post("/properties/search/", body, timeout=120)
            except (urllib.error.URLError, ConnectionError, TimeoutError,
                    OSError) as e:
                if attempt == 5:
                    raise
                print("    network error (%s), retry %d" % (str(e)[:60], attempt + 1))
                time.sleep(3 * (attempt + 1))

    def count(self, filters: dict, address: dict | None = None) -> int:
        r = self.page(filters, address or county_address(FIPS, rich=False))
        return int(r.get("total_results") or 0)

    def ids(self, filters: dict, address: dict | None = None) -> dict:
        """Every row for a query: id -> investor score.

        Pages by ROW OFFSET, but only ever below 10,000 rows. Measured
        2026-09-29: past offset 10,000 the server switches ordering, so ids
        from page 1 reappear and others never surface (a 26,517-row query
        swept to 20,264 unique ids, repeatably). Anything over SPLIT_AT is
        bisected on estimated value, which every query here carries through
        the buy box, and each split must add back to its parent's total."""
        address = address or county_address(FIPS, rich=False)
        lo = int(filters.get("value_min") or 1)
        hi = int(filters.get("value_max") or 700000)
        res = self._ids_range(filters, address, lo, hi)
        res["gaps"] = res.get("gaps", [])
        return res

    def _ids_range(self, filters: dict, address: dict, lo: int, hi: int) -> dict:
        f = dict(filters, value_min=lo, value_max=hi)
        r = self.page(f, address)
        total = int(r.get("total_results") or 0)
        if total > SPLIT_AT and hi - lo >= 2:
            mid = (lo + hi) // 2
            # value_max is inclusive, so [lo, mid] and [mid+1, hi] are disjoint.
            a = self._ids_range(filters, address, lo, mid)
            b = self._ids_range(filters, address, mid + 1, hi)
            gaps = a.get("gaps", []) + b.get("gaps", [])
            if a["total"] + b["total"] != total:
                gaps.append((lo, hi, total, a["total"] + b["total"]))
            return {"total": total, "ids": {**a["ids"], **b["ids"]}, "gaps": gaps}
        out = {}
        idx = 1
        while True:
            rows = r.get("data") or []
            for row in rows:
                if row.get("id") is not None:
                    out[str(row["id"])] = row.get("investor_off_market_score")
            idx += PAGE
            if not rows or idx > total:
                break
            r = self.page(f, address, idx)
        return {"total": total, "ids": out, "gaps": []}


def base_filters() -> dict:
    """Buy box + year built, before the sold exclusion."""
    f = dict(BUY_BOX)
    if YEAR_MIN:
        f["extra_year_built_min"] = YEAR_MIN   # year_built_min is silently ignored
    return f


def rung_extra(keys: list, ai: int | None) -> dict:
    """The ladder's own rung filter (sequential-capture-national.filt_for):
    absentee is preset_absentee_owners, everything else a distressor AND."""
    e = {}
    dist = []
    for k in keys:
        if k == "is_absentee_owners":
            e["preset_absentee_owners"] = True
        elif k.startswith("preset_"):
            e[k] = True
        else:
            dist.append(k)
    if dist:
        e["distressors_include"] = ",".join(sorted(dist))
        e["distressors_include_min_match"] = len(dist)
    if ai is not None:
        e["investor_off_market_score_min"] = ai
    return e


# ------------------------------------------------------------------- probe

def phase_probe(args) -> None:
    s = Search()
    sfr = {"type_single_family": True}
    res = {}

    def c(label, filters, address=None):
        n = s.count(filters, address)
        res[label] = n
        print("  %-52s %s" % (label, f"{n:,}"))
        return n

    print("== key proofs (count delta; shard SFR base_supply is 302,731)")
    base = c("SFR county", sfr)
    c("SFR + bogus key (must equal SFR)", dict(sfr, zz_not_a_filter=True))
    c("SFR + AI 95+ (shard 10,338)", dict(sfr, investor_off_market_score_min=95))
    c("SFR + AI 70+", dict(sfr, investor_off_market_score_min=70))
    c("SFR + AI max 94 (tests _max key)", dict(sfr, investor_off_market_score_max=94))
    bb = c("SFR + buy box", dict(BUY_BOX))
    c("buy box + year_built_min 1940", dict(BUY_BOX, year_built_min=1940))
    c("buy box + year_built_min 1940 max 2010",
      dict(BUY_BOX, year_built_min=1940, year_built_max=2010))
    sold = c("buy box + sold since 2023-01-01",
             dict(BUY_BOX, extra_last_sale_date_min=SOLD_CUTOFF_MIN))
    mx = c("buy box + last sale max 2022-12-31",
           dict(BUY_BOX, extra_last_sale_date_max="2022-12-31"))
    print("    buy box minus sold-since-2023 = %s ; max-filter = %s ; gap = %s"
          % (f"{bb - sold:,}", f"{mx:,}", f"{bb - sold - mx:,}"))
    print("\n== ladder rung sizes, SFR only, vs shard list_size")
    for label, keys, want in [
            ("Judgment Lien", ["is_judgment_liens"], 188),
            ("Free & Clear + Tired Landlord",
             ["is_free_and_clear", "is_tired_landlord"], 13862),
            ("Absentee + Free & Clear",
             ["is_absentee_owners", "is_free_and_clear"], 26360),
            ("Absentee + Vacant",
             ["is_absentee_owners", "is_vacant_property"], 2627)]:
        n = c("rung %s (shard %s)" % (label, f"{want:,}"),
              dict(sfr, **rung_extra(keys, None)))
        res["rung_ratio_" + label] = round(n / want, 3) if want else None

    print("\n== neighborhood address shapes (Kingston Ave)")
    for label, addr in [
            ("thin search+type", {"search": "Kingston Ave", "type": "neighborhood",
                                  "state": "OH"}),
            ("rich + thin", {"name": "Kingston Ave", "state": "OH",
                             "title": "Kingston Ave, OH", "value": "Kingston Ave, OH",
                             "counties": [{"fips": FIPS, "county_name": "Franklin"}],
                             "searchType": "neighborhood", "search": "Kingston Ave",
                             "type": "neighborhood"})]:
        try:
            c("nbhd %s" % label, sfr, addr)
        except Exception as e:  # noqa: BLE001
            print("  nbhd %s -> ERROR %s" % (label, str(e)[:160]))

    print("\n== row offset reach")
    for idx in (10001, 20001, 50001):
        r = s.page(sfr, county_address(FIPS, rich=False), idx)
        rows = r.get("data") or []
        res["offset_%d_rows" % idx] = len(rows)
        print("  result_index %-6d -> %d rows" % (idx, len(rows)))
    row = (s.page(dict(sfr, investor_off_market_score_min=95),
                  county_address(FIPS, rich=False)).get("data") or [{}])[0]
    res["row_keys"] = sorted(row.keys())
    print("\nrow keys:", ", ".join(sorted(row.keys())))
    res["base"] = base
    res["calls"] = s.calls
    _save(PROBE_FILE, res)
    print("\nprobe written: %s (%d calls)" % (PROBE_FILE, s.calls))


# ----------------------------------------------------------- neighborhoods

NBHD_FILE = os.path.join(OUT, "neighborhood_suppress.csv")
AUG_NBHD = os.path.join(os.path.dirname(OUT), "franklin_neighborhood_suppress.csv")


def suppress_reasons(n: dict) -> list:
    """The four rules the Aug 19 ty+1 build scored with (memory:
    project_ty1_franklin_staging). No investor demand, or pricing that puts
    the pocket outside a wholesale buy box."""
    inv = n.get("total_inv_trans_6mo") or 0
    val = n.get("median_home_value") or 0
    sale = n.get("median_sale_price") or 0
    why = []
    if inv == 0:
        why.append("zero investor buys 6mo")
    if inv <= 2 and val >= 400000:
        why.append("<=2 inv buys + value >=$400k")
    if val >= 700000:
        why.append("above $700k buy box")
    if val and sale >= 1.15 * val and inv <= 5:
        why.append("retail pays 15%+ over value, <=5 inv buys")
    return why


def nbhd_filter(names: list) -> dict:
    # A JSON-encoded string, not a list: the SiftMap UI stores
    # JSON.stringify(values) and a raw list 400s "Not a valid string."
    return {"neighborhoods": json.dumps(sorted(names)),
            "neighborhoods_mode": "exclude"} if names else {}


def phase_neighborhoods(args) -> None:
    files = sorted(f for f in os.listdir(OUT)
                   if f.startswith("market_finder_Ohio_Franklin_"))
    if not files:
        raise SystemExit("no fresh Market Finder extraction in %s" % OUT)
    mf = _load(os.path.join(OUT, files[-1]))
    rows = []
    for n in mf["neighborhood_data"]:
        why = suppress_reasons(n)
        if why:
            rows.append({"neighborhood": n["neighborhood"],
                         "inv_trans_6mo": n.get("total_inv_trans_6mo"),
                         "median_home_value": n.get("median_home_value"),
                         "median_sale_price": n.get("median_sale_price"),
                         "homes_sold_last_month": n.get("homes_sold_last_month"),
                         "median_dom": n.get("median_days_on_market"),
                         "suppress_reasons": "; ".join(why)})
    # Every name must resolve in SiftMap, or the exclusion silently does
    # nothing for it. Size each one alone under the buy box.
    s = Search()
    for r in rows:
        r["siftmap_buybox_records"] = s.count(dict(
            BUY_BOX, neighborhoods=json.dumps([r["neighborhood"]]),
            neighborhoods_mode="include"))
    with open(NBHD_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    aug = set()
    if os.path.exists(AUG_NBHD):
        with open(AUG_NBHD, encoding="utf-8") as f:
            aug = {r["neighborhood"] for r in csv.DictReader(f)}
    now = {r["neighborhood"] for r in rows}
    unmatched = [r["neighborhood"] for r in rows if not r["siftmap_buybox_records"]]
    names = [r["neighborhood"] for r in rows]
    bb = s.count(dict(BUY_BOX))
    ex = s.count(dict(BUY_BOX, **nbhd_filter(names)))
    inside = sum(r["siftmap_buybox_records"] for r in rows)
    print("source: %s (%d neighborhoods scored)" % (files[-1], len(mf["neighborhood_data"])))
    print("suppressed: %d  (Aug 19 set: %d; added %d, dropped %d)"
          % (len(rows), len(aug), len(now - aug), len(aug - now)))
    for x in sorted(now - aug):
        print("  + %s" % x)
    for x in sorted(aug - now):
        print("  - %s" % x)
    print("names with zero buy-box records in SiftMap: %d %s"
          % (len(unmatched), unmatched[:12]))
    print("buy box %s -> %s after exclusion (removed %s; sum of singles %s)"
          % (f"{bb:,}", f"{ex:,}", f"{bb - ex:,}", f"{inside:,}"))
    _save(os.path.join(OUT, "neighborhoods.json"), {
        "source": files[-1], "names": names, "unmatched": unmatched,
        "buybox": bb, "buybox_excluded": ex, "sum_singles": inside})


# -------------------------------------------------------------------- walk

def base_walk_filters() -> dict:
    nb = _load(os.path.join(OUT, "neighborhoods.json"))
    if not nb:
        raise SystemExit("run --phase neighborhoods first")
    f = dict(BUY_BOX)
    if YEAR_MIN:
        f["extra_year_built_min"] = YEAR_MIN   # year_built_min is silently ignored
    # No entities (Ty): individuals only. The three owner types partition the
    # buy box exactly (Franklin: 223,059 individual + 28,698 business + 12,638
    # trust = 264,395), so this drops LLCs, corporations AND trusts.
    f["extra_owner_type"] = ["individual"]
    f.update(nbhd_filter(nb["names"]))
    return f


SKIP_KEYS = {"preset_negative_equity", "preset_low_equity"}   # equity goal


def ladder(variant: str) -> list:
    cap = _load(CAPTURE)[FIPS]
    rungs = []
    ai_rungs = sorted({r["ai"] for r in cap["variants"][variant] if r.get("ai")},
                      reverse=True)
    for i, r in enumerate(cap["variants"][variant], 1):
        rung = {"n": i, "label": r["label"], "keys": r["keys"], "ai": r.get("ai"),
                "priority": r.get("priority"), "shard_size": r.get("list_size"),
                "shard_marg_dpd": r.get("marg_dpd"),
                "shard_cum_dpd": r.get("cum_dpd"), "skip": ""}
        if set(r["keys"]) & SKIP_KEYS:
            rung["skip"] = "negative/low equity rung (equity goal)"
        if "preset_owner_occupied" in r["keys"]:
            rung["skip"] = "owner occupied catch-all"
        extra = rung_extra([k for k in r["keys"]], r.get("ai"))
        if r.get("ai"):
            higher = [a for a in ai_rungs if a > r["ai"]]
            if higher:
                extra["investor_off_market_score_max"] = min(higher) - 1
        rung["filters"] = extra
        rungs.append(rung)
    return rungs


def pick_variant(s: Search, want: str) -> str:
    if want != "auto":
        return want
    ai = s.count({"type_single_family": True, "investor_off_market_score_min": 70})
    if ai == 0:
        # The off-market AI score is a PER-COUNTY paid add-on
        # (/api/internal/addon/off_market_investor_ai_score/ settings.counties).
        # ty+2 holds Knox + Blount only and ty+1 holds none, so a Franklin
        # search returns 0 and every row's score is None.
        print("AI supply in Franklin is 0: no account we hold has the off-market "
              "AI add-on for 39049 - walking the lists-only ladder")
        return "lists"
    return "ai"


def phase_count(args) -> None:
    s = Search()
    base = base_walk_filters()
    sold = {"extra_last_sale_date_min": SOLD_CUTOFF_MIN}
    out = {"ai_ladder": [], "filters": base, "sized_by": s.who}
    print("sizing credential: %s" % s.who)
    print("== AI score ladder (off-market investor score), Franklin SFR")
    print("%-6s %9s %9s %9s %9s  %s" % ("AI", "SFR raw", "+buy box", "+indiv/yr",
                                          "net", "shard dpd"))
    cd = _load(os.path.join(OUT, "county-data-39.json"))[FIPS]
    dpd = {c["cutoff"]: c["dpd"] for c in cd["ai_demand"]["cutoffs"]}
    for t in (95, 90, 85, 80, 75, 70, 65, 60):
        a = {"investor_off_market_score_min": t}
        raw = s.count(dict({"type_single_family": True}, **a))
        bb = s.count(dict(BUY_BOX, **a))
        mid = {k: v for k, v in base.items()
               if k not in ("neighborhoods", "neighborhoods_mode")}
        yr = s.count(dict(mid, **a))
        net = s.count(dict(base, **a)) - s.count(dict(base, **a, **sold))
        out["ai_ladder"].append({"cutoff": t, "sfr": raw, "buybox": bb,
                                 "year": yr, "net": net, "shard_dpd": dpd.get(t)})
        print("%-6s %9s %9s %9s %9s  %s" % ("%d+" % t, f"{raw:,}", f"{bb:,}",
                                             f"{yr:,}", f"{net:,}", dpd.get(t, "")))
    variant = pick_variant(s, args.variant)
    out["variant"] = variant
    print("\n== standalone rung sizes under every filter (%s ladder)" % variant)
    print("%-3s %-46s %8s %8s %8s %7s" % ("#", "rung", "shard", "filtered",
                                          "kept", "mdpd"))
    rungs = ladder(variant)
    for r in rungs:
        if r["skip"]:
            r["kept_standalone"] = 0
            print("%-3d %-46s  SKIP: %s" % (r["n"], r["label"][:46], r["skip"]))
            continue
        f = dict(base, **r["filters"])
        a = s.count(f)
        b = s.count(dict(f, **sold))
        r["filtered"] = a
        r["kept_standalone"] = a - b
        print("%-3d %-46s %8s %8s %8s %7s" % (r["n"], r["label"][:46],
              f"{r['shard_size'] or 0:,}", f"{a:,}", f"{a - b:,}",
              r["shard_marg_dpd"]))
    out["rungs"] = rungs
    total = s.count(base)
    out["universe"] = {"filtered": total,
                       "kept": total - s.count(dict(base, **sold))}
    print("\nwhole filtered universe (no segment): %s, %s after the sold exclusion"
          % (f"{total:,}", f"{out['universe']['kept']:,}"))
    floor = s.count(dict(base, extra_year_built_min=1940))
    print("sensitivity: with a 1940 year floor the universe is %s" % f"{floor:,}")
    out["universe"]["with_1940_floor"] = floor
    out["calls"] = s.calls
    _save(os.path.join(OUT, "counts.json"), out)


def phase_sweep(args) -> None:
    counts = _load(os.path.join(OUT, "counts.json"))
    if not counts:
        raise SystemExit("run --phase count first")
    s = Search()
    base = base_walk_filters()
    sold_path = os.path.join(OUT, "sold_ids.json")
    sold = _load(sold_path)
    if sold is None:
        r = s.ids(dict(base, extra_last_sale_date_min=SOLD_CUTOFF_MIN))
        sold = sorted(r["ids"])
        print("sold-since-2023 ids in the filtered universe: %s (reported %s, "
              "split gaps %s)" % (f"{len(sold):,}", f"{r['total']:,}", r["gaps"]))
        # The exclusion is a hard filter: an incomplete sold set lets recent
        # sales into the pull, so refuse rather than warn.
        if len(sold) < r["total"] * 0.995:
            raise SystemExit("sold set incomplete (%d of %d) - refusing to "
                             "select against it" % (len(sold), r["total"]))
        _save(sold_path, sold)
    sold = set(sold)
    expect = {r["n"]: r.get("kept_standalone") for r in counts["rungs"]}
    claimed: dict[str, int] = {}
    walk = []
    for r in counts["rungs"]:
        if r["skip"]:
            continue
        got = s.ids(dict(base, **r["filters"]))
        kept = [i for i in got["ids"] if i not in sold]
        new = [i for i in kept if i not in claimed]
        room = TARGET - len(claimed)
        partial = len(new) > room
        take = new[:room]
        for i in take:
            claimed[i] = r["n"]
        walk.append({"n": r["n"], "label": r["label"], "total": got["total"],
                     "swept": len(got["ids"]), "kept": len(kept), "new": len(new),
                     "taken": len(take), "cum": len(claimed), "partial": partial,
                     "shard_marg_dpd": r["shard_marg_dpd"]})
        print("%-3d %-46s swept %6s kept %6s new %6s taken %6s cum %6s%s"
              % (r["n"], r["label"][:46], f"{len(got['ids']):,}", f"{len(kept):,}",
                 f"{len(new):,}", f"{len(take):,}", f"{len(claimed):,}",
                 "  (partial)" if partial else ""))
        if abs(len(got["ids"]) - got["total"]) > max(3, 0.005 * got["total"]):
            raise SystemExit("rung %d: swept %s of reported %s (gaps %s)"
                             % (r["n"], len(got["ids"]), got["total"], got["gaps"]))
        want = expect.get(r["n"]) or 0
        if abs(len(kept) - want) > max(3, 0.01 * want):
            print("    WARN: kept %s vs count phase %s" % (len(kept), want))
        if len(claimed) >= TARGET:
            break
    _save(os.path.join(OUT, "selection.json"), {
        "variant": counts["variant"], "claimed": claimed, "walk": walk,
        "sized_by": s.who, "calls": s.calls})
    print("\nselected %s unique records across %d rungs (%d search calls)"
          % (f"{len(claimed):,}", len(walk), s.calls))


# -------------------------------------------------------------- pull (ty+1)

LIST_TITLE = "Franklin OH - Top 30K DPD"
COUNTY_TAG = "Franklin OH 39049"
PULL_STATE = os.path.join(OUT, "pull_state_%s.json")


def rung_tag(label: str) -> str:
    return "Top30K " + label.replace("&", "and")


# The only accounts this script may write to, pinned by account uuid so a
# token for the right email on the wrong account still refuses.
TARGETS = {"ty+1@dataflik.com": "e2415c08-d4f1-4c70-9b6c-5dcc70d4a23d",
           "data+staging@dataflik.com": "478569d5-d3e6-41ca-bb34-c3c5885aee1c"}


def _ty1(target: str = "ty+1@dataflik.com"):
    """Session on the destination account. ty+1 resolves from the _api store;
    any other target needs REISIFT_TARGET_JWT (or a file via
    REISIFT_TARGET_JWT_FILE) holding that account's pasted token."""
    from reisift_session import Session
    from siftmap_pull import Map
    if target not in TARGETS:
        raise SystemExit("REFUSING: %s is not an allowed target %s"
                         % (target, sorted(TARGETS)))
    f = os.environ.get("REISIFT_TARGET_JWT_FILE", "").strip()
    if f and not os.environ.get("REISIFT_TARGET_JWT"):
        os.environ["REISIFT_TARGET_JWT"] = open(f, encoding="utf-8").read().strip()
    s = Session(target_email=target)
    ident = s.verify_target()
    if ident["account"] != TARGETS[target]:
        raise SystemExit("REFUSING: token account %s is not %s (%s)"
                         % (ident["account"], target, TARGETS[target]))
    return s, Map(s), ident


class AmbiguousSend(RuntimeError):
    """The request went out and the connection died before a response, so the
    server may or may not have acted on it. Never blindly re-send a write."""


def _post(m, path: str, body: dict, *, idempotent: bool, timeout: int = 240):
    """Map._post plus network-error handling it lacks.

    urllib wraps failures that happen while SENDING in URLError (connect, TLS
    handshake: the request never reached the server, safe to retry). Failures
    while READING the response surface raw (RemoteDisconnected, timeouts): the
    outcome is unknown. Reads retry either way; writes raise AmbiguousSend."""
    import urllib.error
    for attempt in range(6):
        try:
            return m._post(path, body, timeout=timeout)
        except urllib.error.URLError as e:
            if attempt == 5:
                raise
            print("    connection failed before send (%s), retry %d"
                  % (str(getattr(e, "reason", e))[:50], attempt + 1))
        except (ConnectionError, TimeoutError, OSError) as e:
            if not idempotent:
                raise AmbiguousSend(str(e)[:200])
            if attempt == 5:
                raise
            print("    response lost (%s), retry %d" % (str(e)[:50], attempt + 1))
        time.sleep(3 * (attempt + 1))


def ty1_saved(m, ids: list) -> dict:
    """id -> saved_uuid under the destination token (saved is per account)."""
    out = {}
    for i in range(0, len(ids), 100):
        r = _post(m, "/properties/search/", {
            "result_index": 1, "with_boundaries": False, "filters": {},
            "addresses": [{"search": str(x), "type": "ids"} for x in ids[i:i + 100]],
            "polygon": WORLD}, idempotent=True)
        for row in r.get("data") or []:
            if row.get("saved") and row.get("saved_uuid"):
                out[str(row["id"])] = row["saved_uuid"]
    return out


def add_ids(m, ids: list, tags: list):
    """Pull dataflik ids into the target (d4d_call_list route A, verified live).
    filters is EMPTY on purpose: the selection already happened at sizing."""
    return _post(m, "/properties/add-properties-by-query/", {
        "auto_add_enabled": False, "lists": [LIST_TITLE], "tags": tags,
        "replace_owners": False,
        "query": {"result_index": 1, "with_boundaries": False, "filters": {},
                  "addresses": [{"search": str(i), "type": "ids"} for i in ids],
                  "polygon": WORLD}}, idempotent=False)


def phase_pull(args) -> None:
    from siftmap_pull import usage
    sel = _load(os.path.join(OUT, "selection.json"))
    if not sel:
        raise SystemExit("run --phase sweep first")
    s, m, ident = _ty1(args.target)
    labels = {w["n"]: w["label"] for w in sel["walk"]}
    by_rung: dict[int, list] = {}
    for i, n in sel["claimed"].items():
        by_rung.setdefault(int(n), []).append(i)
    ids = list(sel["claimed"])
    saved = ty1_saved(m, ids)
    used, limit = usage(s)
    new = len(ids) - len(saved)
    print("target %s (%s), token exp %s" % (ident["email"], ident["account"],
                                           ident["exp_iso"]))
    print("selection %s ids: %s already in %s, %s new; allowance left %s"
          % (f"{len(ids):,}", f"{len(saved):,}", ident["email"], f"{new:,}",
             f"{limit - used:,}"))
    if new > limit - used:
        raise SystemExit("HARD STOP: %s new records > %s allowance left"
                         % (new, limit - used))
    for n in sorted(by_rung):
        print("  rung %-3d %-46s %6s ids -> tag %r" % (
            n, labels[n][:46], f"{len(by_rung[n]):,}", rung_tag(labels[n])))
    if not args.commit:
        print("DRY RUN: nothing pulled. Re-run with --commit.")
        return
    state_path = PULL_STATE % ident["account"][:8]
    st = _load(state_path, {"done": [], "probe": None, "allowance_before": used,
                            "target": ident["email"]})
    first_n = min(by_rung)
    if not st["probe"]:
        probe_id = next(i for i in by_rung[first_n] if i not in saved) \
            if any(i not in saved for i in by_rung[first_n]) else by_rung[first_n][0]
        try:
            add_ids(m, [probe_id], [rung_tag(labels[first_n]), COUNTY_TAG])
        except AmbiguousSend as e:
            print("probe send lost its response (%s); the landing check decides" % e)
        landed = {}
        for _ in range(30):
            landed = ty1_saved(m, [probe_id])
            if landed:
                break
            time.sleep(20)
        if not landed:
            raise SystemExit("probe id %s did not land in 10 min: staging queue "
                             "stalled? check /api/internal/activity/" % probe_id)
        rec = s.call("/api/internal/property/%s/" % landed[probe_id])
        lists = [x.get("title") if isinstance(x, dict) else x
                 for x in rec.get("lists") or []]
        print("probe %s landed as %s; lists=%s" % (probe_id, landed[probe_id], lists))
        if LIST_TITLE not in lists:
            raise SystemExit("probe landed WITHOUT the list %r - stopping" % LIST_TITLE)
        st["probe"] = {"id": probe_id, "uuid": landed[probe_id]}
        _save(state_path, st)
    done = set(st["done"])
    st.setdefault("uncertain", [])
    probe_id = st["probe"]["id"]
    for n in sorted(by_rung):
        chunk_ids = [i for i in by_rung[n] if i != probe_id]
        for k in range(0, len(chunk_ids), 250):
            key = "%d:%d" % (n, k)
            if key in done:
                continue
            chunk = chunk_ids[k:k + 250]
            if key in st["uncertain"]:
                # A previous send died after going out. Re-send only what has
                # not landed, so a chunk the server did accept is not re-sent.
                landed = ty1_saved(m, chunk)
                chunk = [i for i in chunk if i not in landed]
                print("  %s was uncertain: %d of %d landed, re-sending %d"
                      % (key, len(landed), len(chunk_ids[k:k + 250]), len(chunk)))
            try:
                if chunk:
                    add_ids(m, chunk, [rung_tag(labels[n]), COUNTY_TAG])
            except AmbiguousSend as e:
                print("  %s: response lost after send (%s) - marked uncertain, "
                      "re-run --commit later to reconcile" % (key, e))
                if key not in st["uncertain"]:
                    st["uncertain"].append(key)
                _save(state_path, st)
                time.sleep(5)
                continue
            st["done"].append(key)
            if key in st["uncertain"]:
                st["uncertain"].remove(key)
            _save(state_path, st)
            time.sleep(1.0)
        print("  rung %d submitted (%s ids)" % (n, f"{len(chunk_ids):,}"))
    if st["uncertain"]:
        print("UNCERTAIN chunks (sent, no response): %s - wait for the queue, then "
              "re-run --phase pull --commit to re-send only what did not land"
              % st["uncertain"])
    u2, l2 = usage(s)
    print("all chunks submitted; allowance now %s used of %s" % (f"{u2:,}", f"{l2:,}"))


def phase_qa(args) -> None:
    sel = _load(os.path.join(OUT, "selection.json"))
    s, m, ident = _ty1(args.target)
    ids = list(sel["claimed"])
    saved = ty1_saved(m, ids)
    lists = s.call("/api/internal/list/?limit=999").get("results", [])
    uuid = next((x["uuid"] for x in lists if x["title"] == LIST_TITLE), None)
    n_list = 0
    if uuid:
        r = s.call("/api/internal/property/", method="POST", method_override="GET",
                   body={"limit": 1, "offset": 0,
                         "query": {"must": {"any_lists": [uuid]}}})
        n_list = int(r.get("count", 0))
    print("landed (saved in %s): %s of %s; list %r holds %s"
          % (ident["email"], f"{len(saved):,}", f"{len(ids):,}", LIST_TITLE,
             f"{n_list:,}"))
    tags = {x["title"]: x["uuid"] for x in s.call(
        "/api/internal/tag/?offset=0&limit=10000&ordering=title").get("results", [])}
    per_rung = {}
    want = {}
    for i, n in sel["claimed"].items():
        want[int(n)] = want.get(int(n), 0) + 1
    for w in sel["walk"]:
        tag = rung_tag(w["label"])
        got = 0
        if tag in tags:
            r = s.call("/api/internal/property/", method="POST", method_override="GET",
                       body={"limit": 1, "offset": 0,
                             "query": {"must": {"any_tags": [tags[tag]]}}})
            got = int(r.get("count", 0))
        per_rung[tag] = {"selected": want.get(w["n"], 0), "tagged": got}
        print("  %-52s selected %6s tagged %6s" % (tag[:52], f"{want.get(w['n'], 0):,}",
                                                   f"{got:,}"))
        time.sleep(0.4)
    import random
    random.seed(7)
    sample = random.sample(sorted(saved.values()), min(25, len(saved)))
    fails = []
    for u in sample:
        rec = s.call("/api/internal/property/%s/" % u)
        yr = rec.get("year")
        last = rec.get("last_sold") or ""
        st = rec.get("structure_type") or ""
        bad = []
        if YEAR_MIN and yr and int(yr) < YEAR_MIN:
            bad.append("year %s" % yr)
        if last and str(last)[:10] >= SOLD_CUTOFF_MIN:
            bad.append("sold %s" % last)
        if st and "single" not in st.lower():
            bad.append("structure %s" % st)
        if bad:
            fails.append((u, bad))
        time.sleep(0.4)
    print("spot-check %d records: %d failures %s" % (len(sample), len(fails), fails[:5]))
    _save(os.path.join(OUT, "qa_%s.json" % ident["account"][:8]), {
        "target": ident["email"], "landed": len(saved), "selected": len(ids),
        "list_count": n_list, "per_rung": per_rung, "fails": fails})


# ------------------------------------------------ Trestle estimate (phones)

# Same rules as d4d_call_list.is_eligible, so the estimate and the real run
# count the same numbers: unscored, not litigator-tagged, not dispositioned,
# a real 10-digit number. Phones are OWNER-level in DataSift, so an owner with
# several properties on the list is one set of numbers, billed once.
TIER_TAGS = {"Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop"}
BAD_PHONE_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
TRESTLE_COST = 0.015          # phone_validator.COST_PER_PHONE, matches billing
TRESTLE_FREE_MONTHLY = 1000   # trestle_router free key, resets monthly


def _phone_tags(p: dict) -> list:
    out = []
    for t in p.get("tags") or []:
        if isinstance(t, dict):
            t = t.get("title") or t.get("name") or t.get("tag")
        if t:
            out.append(str(t))
    return out


def classify_phone(p: dict) -> str:
    tags = _phone_tags(p)
    if set(tags) & TIER_TAGS:
        return "already_tiered"
    if "DNC" in tags:
        return "litigator_tagged"
    if (p.get("status") or "UNKNOWN").upper() in BAD_PHONE_STATUSES:
        return "dispositioned"
    digits = "".join(ch for ch in str(p.get("number") or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return "invalid"
    return "eligible"


def _list_owners(s, list_uuid: str) -> dict:
    """record uuid -> owner uuid for every record on the list, from the cheap
    records search (10,000-row offset cap, so page per rung tag)."""
    tags = {x["title"]: x["uuid"] for x in s.call(
        "/api/internal/tag/?offset=0&limit=10000&ordering=title").get("results", [])}
    rung = {t: u for t, u in tags.items() if t.startswith("Top30K ")}
    out = {}
    for title, tag_uuid in sorted(rung.items()):
        off = 0
        while True:
            r = s.call("/api/internal/property/", method="POST", method_override="GET",
                       body={"limit": 100, "offset": off, "query": {"must": {
                           "any_lists": [list_uuid], "any_tags": [tag_uuid]}}})
            rows = r.get("results") or []
            for x in rows:
                ow = x.get("owner") or {}
                out[x["uuid"]] = ow.get("uuid") if isinstance(ow, dict) else None
            off += len(rows)
            if not rows or off >= int(r.get("count") or 0):
                break
            time.sleep(0.3)
    return out


def phase_phones(args) -> None:
    """Estimate how many numbers on the pulled list still need Trestle, and
    what that costs. Reads only: a seeded sample of full records for the phone
    detail, plus every record's owner for the dedupe."""
    import math
    import random
    s, m, ident = _ty1(args.target)
    lists = s.call("/api/internal/list/?limit=999").get("results", [])
    list_uuid = next(x["uuid"] for x in lists if x["title"] == LIST_TITLE)
    n_list = int(s.call("/api/internal/property/", method="POST", method_override="GET",
                        body={"limit": 1, "offset": 0, "query": {"must": {
                            "any_lists": [list_uuid]}}}).get("count", 0))
    owners = _list_owners(s, list_uuid)
    uniq_owners = {o for o in owners.values() if o}
    print("list %s records; owner map %s records, %s unique owners"
          % (f"{n_list:,}", f"{len(owners):,}", f"{len(uniq_owners):,}"))
    # Sample OWNERS, not records: the bill is per unique number and numbers
    # hang off the owner. One record per sampled owner gives its phones.
    rec_for_owner = {}
    for rec, own in owners.items():
        if own:
            rec_for_owner.setdefault(own, rec)
    random.seed(args.seed)
    pool = sorted(rec_for_owner)
    sample = random.sample(pool, min(args.sample, len(pool)))
    per_owner = []
    numbers_seen = {}
    for k, own in enumerate(sample, 1):
        d = s.call("/api/internal/property/%s/" % rec_for_owner[own])
        phones = ((d.get("owner") or {}).get("phones")) or []
        c = {"phones": len(phones), "already_tiered": 0, "litigator_tagged": 0,
             "dispositioned": 0, "invalid": 0, "eligible": 0}
        for p in phones:
            cls = classify_phone(p)
            c[cls] += 1
            if cls == "eligible":
                numbers_seen.setdefault("".join(ch for ch in str(p.get("number"))
                                                if ch.isdigit())[-10:], set()).add(own)
        per_owner.append(c)
        if k % 100 == 0:
            print("  sampled %d/%d owners" % (k, len(sample)))
        time.sleep(0.35)
    n = len(per_owner)
    U = len(uniq_owners)

    def est(key):
        vals = [c[key] for c in per_owner]
        mean = sum(vals) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / max(1, n - 1))
        half = 1.96 * sd / math.sqrt(n) * math.sqrt(1 - n / U)   # finite-pop corr.
        return mean * U, max(0.0, (mean - half) * U), (mean + half) * U, mean
    # Numbers shared by two different sampled owners (spouses on separate
    # owner records, a shared landline) are billed once.
    shared = sum(len(v) - 1 for v in numbers_seen.values() if len(v) > 1)
    elig_total = sum(c["eligible"] for c in per_owner)
    share_rate = shared / elig_total if elig_total else 0.0
    out = {"target": ident["email"], "list_records": n_list, "unique_owners": U,
           "sample_owners": n, "seed": args.seed, "cross_owner_share_rate": share_rate}
    print("\nper unique owner (sample of %d, scaled to %s owners):" % (n, f"{U:,}"))
    for key in ("phones", "already_tiered", "litigator_tagged", "dispositioned",
                "invalid", "eligible"):
        tot, lo, hi, mean = est(key)
        out[key] = {"est": round(tot), "lo95": round(lo), "hi95": round(hi),
                    "per_owner": round(mean, 2)}
        print("  %-17s %8s   (95%%: %s - %s)   %.2f per owner"
              % (key, f"{round(tot):,}", f"{round(lo):,}", f"{round(hi):,}", mean))
    e, lo, hi, _ = est("eligible")
    adj = 1 - share_rate
    e, lo, hi = e * adj, lo * adj, hi * adj
    free = TRESTLE_FREE_MONTHLY
    def cost(x):
        return max(0.0, x - free) * TRESTLE_COST
    out["to_score"] = {"est": round(e), "lo95": round(lo), "hi95": round(hi)}
    out["cost"] = {"est": round(cost(e), 2), "lo95": round(cost(lo), 2),
                   "hi95": round(cost(hi), 2), "per_number": TRESTLE_COST,
                   "free_monthly_applied": free,
                   "all_paid_est": round(e * TRESTLE_COST, 2)}
    print("\nnumbers to Trestle (deduped, %.1f%% shared across owners): %s  (95%%: %s - %s)"
          % (100 * share_rate, f"{round(e):,}", f"{round(lo):,}", f"{round(hi):,}"))
    print("cost at $%.3f after %s free lookups: $%s  (95%%: $%s - $%s); all-paid $%s"
          % (TRESTLE_COST, f"{free:,}", f"{cost(e):,.2f}", f"{cost(lo):,.2f}",
             f"{cost(hi):,.2f}", f"{e * TRESTLE_COST:,.2f}"))
    _save(os.path.join(OUT, "trestle_estimate.json"), out)


PHONES_PATH = os.path.join(OUT, "phones_by_owner.json")
OWNERS_PATH = os.path.join(OUT, "list_owners.json")


def phase_harvest(args) -> None:
    """Read every unique owner's phones off the CRM. Free, read-only,
    resumable: one record detail per unique owner (phones are owner-level)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    s, m, ident = _ty1(args.target)
    lists = s.call("/api/internal/list/?limit=999").get("results", [])
    list_uuid = next(x["uuid"] for x in lists if x["title"] == LIST_TITLE)
    owners = _load(OWNERS_PATH)
    if owners is None:
        owners = _list_owners(s, list_uuid)
        _save(OWNERS_PATH, owners)
    rec_for_owner = {}
    for rec, own in sorted(owners.items()):
        if own:
            rec_for_owner.setdefault(own, rec)
    got = _load(PHONES_PATH, {})
    todo = [o for o in sorted(rec_for_owner) if o not in got]
    print("harvest: %s unique owners, %s cached, %s to read"
          % (f"{len(rec_for_owner):,}", f"{len(got):,}", f"{len(todo):,}"))
    lock = threading.Lock()
    pace = {"t": 0.0}

    def read(own):
        with lock:   # ~2.5 req/s aggregate: /api/internal/ throttles harder above
            wait = pace["t"] + 0.4 - time.time()
            if wait > 0:
                time.sleep(wait)
            pace["t"] = time.time()
        d = s.call("/api/internal/property/%s/" % rec_for_owner[own])
        ow = d.get("owner") or {}
        return own, {"record": rec_for_owner[own], "owner_check": ow.get("uuid"),
                     "phones": ow.get("phones") or []}

    failed = 0
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(read, o) for o in todo]
        for k, f in enumerate(as_completed(futs), 1):
            try:
                own, v = f.result()
                got[own] = v
            except Exception as e:  # noqa: BLE001
                failed += 1
                if failed <= 20:
                    print("  read failed: %s" % str(e)[:120])
            if k % 500 == 0:
                _save(PHONES_PATH, got)
                print("  %s/%s owners read (%d failed)" % (f"{k:,}", f"{len(todo):,}", failed))
    _save(PHONES_PATH, got)
    mism = sum(1 for o, v in got.items() if v.get("owner_check") and v["owner_check"] != o)
    nums = {}
    for o, v in got.items():
        for p in v["phones"]:
            if classify_phone(p) == "eligible":
                nums.setdefault("".join(ch for ch in str(p["number"]) if ch.isdigit())[-10:], set()).add(o)
    print("harvest done: %s owners, %d failed, %d owner mismatches, %s unique eligible numbers"
          % (f"{len(got):,}", failed, mism, f"{len(nums):,}"))
    if failed:
        print("  re-run --phase harvest to retry the %d failures (cached owners are skipped)" % failed)


SCORES_PATH = os.path.join(OUT, "trestle_scores.json")
WRITEBACK_PATH = os.path.join(OUT, "writeback_state.json")
LITIGATOR_TIER = "DNC"   # d4d_call_list convention: litigator risk replaces the tier


def _digits10(raw) -> str:
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def _trestle_key() -> str:
    """The key Ty named for billing. Read from a file so it never sits in a
    command line, the repo or .env (it matches .env TRESTLE_PAID_API_KEY)."""
    import hashlib
    f = os.environ.get("TRESTLE_KEY_FILE", "").strip()
    if not f:
        raise SystemExit("set TRESTLE_KEY_FILE to the file holding the Trestle key")
    k = open(f, encoding="utf-8").read().strip()
    print("trestle key fingerprint %s (paid account, no free-key routing)"
          % hashlib.sha256(k.encode()).hexdigest()[:10])
    return k


def _harvested() -> tuple[dict, int]:
    owners = _load(OWNERS_PATH) or {}
    uniq = {o for o in owners.values() if o}
    got = _load(PHONES_PATH, {})
    return got, len(uniq - set(got))


def eligible_by_number(got: dict) -> dict:
    out = {}
    for own, v in got.items():
        for p in v["phones"]:
            if classify_phone(p) == "eligible":
                out.setdefault(_digits10(p["number"]), []).append(own)
    return out


def _score_one(num: str, key: str) -> dict:
    import phone_validator as pv
    data = pv.call_trestle(num, key, add_litigator=True)
    if data.get("error"):
        return {"error": str(data["error"])[:120]}
    score = data.get("activity_score")
    lit = None
    add = data.get("add_ons") or {}
    if isinstance(add, dict) and isinstance(add.get("litigator_checks"), dict):
        lit = add["litigator_checks"].get("phone.is_litigator_risk")
    tier = LITIGATOR_TIER if lit else pv.assign_tier(score, pv.DEFAULT_TIERS)
    return {"score": score, "tier": tier, "line_type": data.get("line_type"),
            "is_valid": data.get("is_valid"), "litigator": bool(lit),
            "when": time.strftime("%Y-%m-%dT%H:%M:%S")}


def phase_score(args) -> None:
    """Trestle-score every unique eligible number. --commit spends."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    got, missing = _harvested()
    if missing and not args.allow_partial:
        raise SystemExit("harvest incomplete: %d owners not read yet "
                         "(re-run --phase harvest, or --allow-partial)" % missing)
    elig = eligible_by_number(got)
    cache = _load(SCORES_PATH, {})
    todo = sorted(n for n in elig if not (cache.get(n) or {}).get("tier"))
    cost = len(todo) * TRESTLE_COST
    print("score: %s unique eligible numbers from %s owners; %s cached; %s to score; "
          "cost $%s at $%.3f (paid key, no free routing)"
          % (f"{len(elig):,}", f"{len(got):,}", f"{len(elig) - len(todo):,}",
             f"{len(todo):,}", f"{cost:,.2f}", TRESTLE_COST))
    if cost > args.max_spend:
        raise SystemExit("REFUSING: $%.2f exceeds the approved cap $%.2f - re-confirm "
                         "with Ty, then pass --max-spend" % (cost, args.max_spend))
    if not args.commit or not todo:
        print("DRY RUN: nothing scored." if not args.commit else "nothing to score.")
        return
    key = _trestle_key()
    # Paid probes before the pool: a bad key, an empty wallet or a changed
    # response shape should cost cents, not the batch. Probe the home area code
    # (an unassigned code like 200 is a legitimate "is_valid: false" answer,
    # which is what tripped the first version of this check on 2026-10-02).
    # Any ERROR stops; up to 5 tries must yield at least one real score.
    home = [n for n in todo if n.startswith("614")][:5] or todo[:5]
    probed, usable = [], False
    for n in home:
        cache[n] = _score_one(n, key)
        probed.append(n)
        print("probe %s -> %s" % (n[:3] + "***" + n[-4:], cache[n]))
        if cache[n].get("error"):
            _save(SCORES_PATH, cache)
            raise SystemExit("probe ERROR (%s) - key, wallet or API problem; "
                             "stopping before the batch" % cache[n]["error"])
        if isinstance(cache[n].get("score"), (int, float)):
            usable = True
            break
    _save(SCORES_PATH, cache)
    if not usable:
        raise SystemExit("%d probes answered without an activity score - stopping "
                         "before the batch" % len(probed))
    rest = [n for n in todo if n not in set(probed)]
    done = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_score_one, n, key): n for n in rest}
        for f in as_completed(futs):
            n = futs[f]
            try:
                cache[n] = f.result()
            except Exception as e:  # noqa: BLE001
                cache[n] = {"error": str(e)[:120]}
            done += 1
            if cache[n].get("error"):
                errors += 1
            if done % 500 == 0:
                _save(SCORES_PATH, cache)
                print("  scored %s/%s (%d errors)" % (f"{done:,}", f"{len(rest):,}", errors),
                      flush=True)
                if done >= 500 and errors > done * 0.2:
                    for fut in futs:
                        fut.cancel()
                    _save(SCORES_PATH, cache)
                    raise SystemExit("STOPPED: %d of %d calls errored (>20%%)" % (errors, done))
    _save(SCORES_PATH, cache)
    tiers = {}
    for n in elig:
        t = (cache.get(n) or {}).get("tier") or "error"
        tiers[t] = tiers.get(t, 0) + 1
    print("score done: %s scored this run, %d errors; tiers %s"
          % (f"{done + 1:,}", errors, json.dumps(tiers)))


def _tier_payload(p: dict, tier: str) -> dict:
    """The full phone object upsert-phones takes, carrying exactly one tier
    (d4d_call_list.payload_for_tier: strip any other tier first)."""
    tags = [t for t in _phone_tags(p) if t not in TIER_TAGS and t != LITIGATOR_TIER]
    tags.append(tier)
    return {"number": _digits10(p.get("number")) or p.get("number"),
            "type": p.get("type") or "UNKNOWN", "tags": sorted(tags),
            "status": (p.get("status") or "UNKNOWN").upper(),
            "is_connected": p.get("is_connected"),
            "verified": p.get("verified", False)}


def _plan_writes(got: dict, cache: dict) -> dict:
    plan = {}
    for own, v in got.items():
        phones, expect = [], {}
        for p in v["phones"]:
            if classify_phone(p) != "eligible":
                continue
            num = _digits10(p["number"])
            tier = (cache.get(num) or {}).get("tier")
            if not tier or tier == "Unknown":
                continue
            phones.append(_tier_payload(p, tier))
            expect[num] = tier
        if phones:
            plan[own] = {"record": v["record"], "phones": phones, "expect": expect}
    return plan


def _check_owner(s, v: dict) -> list:
    rec = s.call("/api/internal/property/%s/" % v["record"])
    have = {_digits10(p.get("number")): p for p in
            ((rec.get("owner") or {}).get("phones") or [])}
    bad = []
    for num, tier in v["expect"].items():
        p = have.get(num)
        tiers = [t for t in _phone_tags(p or {}) if t in TIER_TAGS or t == LITIGATOR_TIER]
        if not p or tiers != [tier]:
            bad.append((num[-4:], tier, tiers))
    return bad


def phase_writeback(args) -> None:
    """Write each scored tier onto the CRM phone as a tag. --commit writes."""
    import random
    s, m, ident = _ty1(args.target)
    got, _ = _harvested()
    cache = _load(SCORES_PATH, {})
    plan = _plan_writes(got, cache)
    # Track WRITTEN NUMBERS per owner, not owners: scoring can finish in more
    # than one pass (a wallet top-up mid-run), and an owner marked "done" after
    # the first pass would never get its later-scored numbers written.
    st = _load(WRITEBACK_PATH, {"written": {}, "failed": {}})
    written = {o: set(v) for o, v in st["written"].items()}
    pending = {}
    for own, v in plan.items():
        new = [p for p in v["phones"] if p["number"] not in written.get(own, set())]
        if new:
            pending[own] = new
    todo = sorted(pending)
    print("writeback: %s owners / %s phones planned; %s owners / %s phones still to write"
          % (f"{len(plan):,}", f"{sum(len(v['phones']) for v in plan.values()):,}",
             f"{len(todo):,}", f"{sum(len(v) for v in pending.values()):,}"))
    if not args.commit or not todo:
        print("DRY RUN: nothing written." if not args.commit else "nothing to write.")
        return

    def write(own):
        s.call("/api/internal/owner/%s/upsert-phones/" % own, method="POST",
               body={"phones": pending[own]})
        written.setdefault(own, set()).update(p["number"] for p in pending[own])

    def persist():
        st["written"] = {o: sorted(v) for o, v in written.items()}
        _save(WRITEBACK_PATH, st)

    if not written:   # first owner alone, then read it back before the batch
        own = todo[0]
        write(own)
        bad = _check_owner(s, plan[own])
        print("probe owner %s: %s" % (own[:8], "OK" if not bad else bad))
        if bad:
            raise SystemExit("probe write did not read back as planned - stopping")
        persist()
        todo = todo[1:]
    for k, own in enumerate(todo, 1):
        try:
            write(own)
            st["failed"].pop(own, None)
        except Exception as e:  # noqa: BLE001
            st["failed"][own] = str(e)[:160]
        if k % 200 == 0:
            persist()
            print("  %s/%s owners written (%d failed)" % (f"{k:,}", f"{len(todo):,}",
                                                         len(st["failed"])), flush=True)
        time.sleep(0.4)
    persist()
    done = sorted(o for o in written if o in plan)
    random.seed(41)
    sample = random.sample(done, min(args.readback, len(done)))
    mism = 0
    for own in sample:
        bad = _check_owner(s, plan[own])
        mism += len(bad)
        if bad:
            print("  MISMATCH owner %s: %s" % (own[:8], bad[:3]))
        time.sleep(0.4)
    print("writeback done: %s owners written, %d failed; read-back %d owners, %d mismatches"
          % (f"{len(done):,}", len(st["failed"]), len(sample), mism))


# ------------------------------------------------- call list export (Dial 1/2)

LIST_ROWS_PATH = os.path.join(OUT, "list_rows.json")
CRM_URL = "https://app.reisift.io/records/%s"     # d4d_call_list convention
CALL_TIERS = ("Dial First", "Dial Second")
PHONE_SLOTS = 6


def _list_rows(s, list_uuid: str) -> dict:
    """record uuid -> owner name, mailing and property address, off the cheap
    records search (paged per rung tag: the offset cap is 10,000)."""
    tags = {x["title"]: x["uuid"] for x in s.call(
        "/api/internal/tag/?offset=0&limit=10000&ordering=title").get("results", [])}
    out = {}
    for title, tag_uuid in sorted((t, u) for t, u in tags.items() if t.startswith("Top30K ")):
        off = 0
        while True:
            r = s.call("/api/internal/property/", method="POST", method_override="GET",
                       body={"limit": 100, "offset": off, "query": {"must": {
                           "any_lists": [list_uuid], "any_tags": [tag_uuid]}}})
            rows = r.get("results") or []
            for x in rows:
                ow = x.get("owner") or {}
                oa = ow.get("address") or {}
                a = x.get("address") or {}
                out[x["uuid"]] = {
                    "owner_uuid": ow.get("uuid"), "first": ow.get("first_name") or "",
                    "last": ow.get("last_name") or "", "company": ow.get("company") or "",
                    "mail_street": oa.get("street") or "", "mail_city": oa.get("city") or "",
                    "mail_state": oa.get("state") or "",
                    "mail_zip": str(oa.get("zip5") or oa.get("postal_code") or "")[:5],
                    "street": a.get("street") or "", "city": a.get("city") or "",
                    "state": a.get("state") or "",
                    "zip": str(a.get("zip5") or a.get("postal_code") or "")[:5]}
            off += len(rows)
            if not rows or off >= int(r.get("count") or 0):
                break
            time.sleep(0.3)
    return out


def _fmt_phone(n: str) -> str:
    return "(%s) %s-%s" % (n[:3], n[3:6], n[6:]) if len(n) == 10 else n


def _clean(v) -> str:
    """House rule: no em or en dashes anywhere in a deliverable."""
    return str(v if v is not None else "").replace("—", ", ").replace("–", "-")


def phase_export(args) -> None:
    """Call list of every owner with a Dial First / Dial Second number, plus
    a summary of what is in the file versus left out (unscored numbers)."""
    import csv as _csv
    from collections import Counter, defaultdict
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    got = _load(PHONES_PATH, {})
    cache = _load(SCORES_PATH, {})
    owners_map = _load(OWNERS_PATH, {})
    sel = _load(os.path.join(OUT, "selection.json"))
    srows = _load(os.path.join(OUT, "selection_rows.json"), {})
    aq = _load(os.path.join(OUT, "address_quality.json"), {"zero_number": [], "off_zip": []})
    rows = _load(LIST_ROWS_PATH)
    if rows is None and not args.offline:
        s, m, ident = _ty1(args.target)
        lists = s.call("/api/internal/list/?limit=999").get("results", [])
        list_uuid = next(x["uuid"] for x in lists if x["title"] == LIST_TITLE)
        rows = _list_rows(s, list_uuid)
        _save(LIST_ROWS_PATH, rows)
    rows = rows or {}
    if rows:
        drift = sum(1 for rec, own in owners_map.items()
                    if rec in rows and rows[rec]["owner_uuid"] and rows[rec]["owner_uuid"] != own)
        print("list rows: %s records with names; %d owner changes since the harvest"
              % (f"{len(rows):,}", drift))

    # record -> (ladder rung number, segment label) for priority ordering
    labels = {w["n"]: w["label"] for w in sel["walk"]}
    rung_of_rec, did_of_rec = {}, {}
    for did, n in sel["claimed"].items():
        u = (srows.get(did) or {}).get("saved_uuid")
        if u:
            rung_of_rec[u] = (int(n), labels[int(n)])
            did_of_rec[u] = did
    bad_dids = set(aq["zero_number"]) | set(aq["off_zip"])
    recs_by_owner = defaultdict(list)
    for rec, own in owners_map.items():
        if own:
            recs_by_owner[own].append(rec)

    owners_out, phone_rows = [], []
    status = Counter()
    tier_ct = Counter()
    for own, v in got.items():
        elig = [p for p in v["phones"] if classify_phone(p) == "eligible"]
        if not elig:
            status["no_phones"] += 1
            continue
        call, pending = [], 0
        for p in elig:
            n = _digits10(p["number"])
            c = cache.get(n) or {}
            t = c.get("tier")
            tier_ct[t or "not scored yet"] += 1
            if not t:
                pending += 1
            elif t in CALL_TIERS:
                call.append({"num": n, "type": (p.get("type") or "").title(), "tier": t,
                             "score": c.get("score")})
        status["fully scored" if not pending else
               ("not scored yet" if pending == len(elig) else "partly scored")] += 1
        if not call:
            continue
        call.sort(key=lambda x: (CALL_TIERS.index(x["tier"]), -(x["score"] or 0)))
        recs = sorted(recs_by_owner.get(own, [v["record"]]),
                      key=lambda r: rung_of_rec.get(r, (999, ""))[0])
        primary = recs[0]
        rn, seg = rung_of_rec.get(primary, (999, ""))
        info = rows.get(primary, {})
        others = [rows.get(r, {}).get("street", "") for r in recs[1:]]
        bad = did_of_rec.get(primary) in bad_dids
        owners_out.append({
            "_rung": rn, "_best": call[0]["score"] or 0, "owner": own,
            "Segment": seg, "Owner First": info.get("first", ""), "Owner Last": info.get("last", ""),
            "Property Address": info.get("street", ""), "City": info.get("city", ""),
            "ZIP": info.get("zip", ""),
            "Other Properties On List": len(recs) - 1,
            "Other Property Addresses": "; ".join(o for o in others if o),
            "Mailing Address": info.get("mail_street", ""), "Mailing City": info.get("mail_city", ""),
            "Mailing State": info.get("mail_state", ""), "Mailing ZIP": info.get("mail_zip", ""),
            "phones": call, "Numbers Not Scored": pending,
            "Address Note": ("County property address is incomplete; use the mailing address"
                             if bad else ""),
            "DataSift Record": CRM_URL % primary})
    owners_out.sort(key=lambda o: (o["_rung"], -o["_best"], -len(o["phones"])))
    for i, o in enumerate(owners_out, 1):
        o["Priority"] = i
        for p in o["phones"]:
            phone_rows.append({
                "Priority": i, "Owner Name": ("%s %s" % (o["Owner First"], o["Owner Last"])).strip(),
                "Property Address": o["Property Address"], "City": o["City"], "ZIP": o["ZIP"],
                "Phone": _fmt_phone(p["num"]), "Type": p["type"], "Tier": p["tier"],
                "Trestle Score": p["score"], "Segment": o["Segment"],
                "Address Note": o["Address Note"], "DataSift Record": o["DataSift Record"]})

    n_props = sum(1 + o["Other Properties On List"] for o in owners_out)
    # Summary counts are UNIQUE numbers; the file has one row per owner-number
    # pair, so a number shared by two owners (spouses, co-owners) shows twice.
    uniq_tier = Counter((cache.get(n) or {}).get("tier") or "not scored yet"
                        for n in eligible_by_number(got))
    call_uniq = {p["Phone"] for p in phone_rows}
    call_uniq_t = Counter()
    for p in phone_rows:
        call_uniq_t[(p["Tier"], p["Phone"])] = 1
    n_first = sum(1 for (t_, _n) in call_uniq_t if t_ == "Dial First")
    n_second = sum(1 for (t_, _n) in call_uniq_t if t_ == "Dial Second")
    unscored_nums = sum(1 for n in eligible_by_number(got) if not (cache.get(n) or {}).get("tier"))
    scored_nums = sum(1 for n in eligible_by_number(got) if (cache.get(n) or {}).get("tier"))
    stamp = time.strftime("%Y-%m-%d")
    base = os.path.join(OUT, "Franklin_Top30K_Call_List_Dial1-2_%s" % stamp)

    # ---- workbook
    wb = Workbook()
    navy, grey = "0A1130", "5B6476"
    thin = Side(style="thin", color="D5D9E0")
    hdr_font = Font(bold=True, color=navy)
    hdr_fill = PatternFill("solid", fgColor="F2F4F7")

    def table(ws, header, data, widths):
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = hdr_font, hdr_fill
            c.border = Border(bottom=thin)
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in data:
            ws.append([_clean(r.get(h, "")) if isinstance(r.get(h, ""), str) else r.get(h, "")
                       for h in header])
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        ws.row_dimensions[1].height = 30

    sm = wb.active
    sm.title = "Summary"
    sm.column_dimensions["A"].width = 46
    sm.column_dimensions["B"].width = 16
    sm.column_dimensions["C"].width = 70
    lines = [
        ("Franklin County OH, Top 30K call list (Dial 1 and Dial 2)", None, None),
        ("Built %s from the DataSift list \"%s\"" % (stamp, LIST_TITLE), None, None),
        (None, None, None),
        ("IN THIS FILE", None, None),
        ("Owners to call", len(owners_out), "Each owner once; phones are owner-level, so one call covers all their properties"),
        ("Properties covered", n_props, "Includes owners with more than one property on the list"),
        ("Phone numbers (unique)", len(call_uniq), "%s rows on the Phones tab: a number shared by two owners is listed under each" % f"{len(phone_rows):,}"),
        ("  Dial First (score 81-100)", n_first, "Call these first"),
        ("  Dial Second (score 61-80)", n_second, ""),
        ("Rows with an incomplete county address", sum(1 for o in owners_out if o["Address Note"]), "Use the mailing address on these"),
        (None, None, None),
        ("SCORED BUT NOT IN THIS FILE", None, None),
        ("Dial Third (41-60)", uniq_tier["Dial Third"], "Lower answer odds; held for a later pass"),
        ("Dial Fourth (21-40)", uniq_tier["Dial Fourth"], ""),
        ("Drop (0-20)", uniq_tier["Drop"], "Likely dead or inactive numbers"),
        ("Litigator risk (tagged DNC)", uniq_tier["DNC"], "Do not call"),
        ("Invalid numbers", uniq_tier["Unknown"], "Trestle could not score them"),
        (None, None, None),
        ("NOT SCORED, NOT IN THIS FILE", None, None),
        ("Numbers not scored", unscored_nums, "Never scored, so they are left out of this file"),
        ("Owners with at least one number not scored", status["partly scored"] + status["not scored yet"], "Some are in this file on their scored Dial 1/2 numbers; the unscored ones are left out"),
        ("  Owners with nothing scored", status["not scored yet"], "Not in this file"),
        ("Owners with no phone number", status["no_phones"], "Skip traced, nothing found"),
        (None, None, None),
        ("WHOLE LIST", None, None),
        ("Properties on the list", len(owners_map), "Single family, individual owners, no sale since Jan 1 2023, $1-700K, off market"),
        ("Unique owners", len(got), ""),
        ("Unique phone numbers", scored_nums + unscored_nums, "%s scored" % f"{scored_nums:,}"),
    ]
    for a, b, c in lines:
        sm.append([_clean(a) if a else None, b, _clean(c) if c else None])
    sm["A1"].font = Font(bold=True, size=14, color=navy)
    sm["A2"].font = Font(color=grey)
    for r in range(3, sm.max_row + 1):
        a = sm.cell(r, 1)
        if a.value and str(a.value).isupper():
            a.font = Font(bold=True, color=navy)
            a.border = Border(bottom=thin)
        b = sm.cell(r, 2)
        if isinstance(b.value, int):
            b.number_format = "#,##0"
        sm.cell(r, 3).font = Font(color=grey)
        sm.cell(r, 3).alignment = Alignment(wrap_text=True, vertical="top")

    cl = wb.create_sheet("Call List")
    head = ["Priority", "Segment", "Owner First", "Owner Last", "Property Address", "City", "ZIP",
            "Other Properties On List", "Other Property Addresses", "Mailing Address",
            "Mailing City", "Mailing State", "Mailing ZIP"]
    for i in range(1, PHONE_SLOTS + 1):
        head += ["Phone %d" % i, "Phone %d Type" % i, "Phone %d Tier" % i, "Phone %d Score" % i]
    head += ["More Dial 1/2 Numbers", "Numbers Not Scored", "Address Note", "DataSift Record"]
    flat = []
    for o in owners_out:
        r = {k: o[k] for k in o if not k.startswith("_") and k != "phones"}
        for i, p in enumerate(o["phones"][:PHONE_SLOTS], 1):
            r["Phone %d" % i] = _fmt_phone(p["num"])
            r["Phone %d Type" % i] = p["type"]
            r["Phone %d Tier" % i] = p["tier"]
            r["Phone %d Score" % i] = p["score"]
        r["More Dial 1/2 Numbers"] = max(0, len(o["phones"]) - PHONE_SLOTS)
        flat.append(r)
    widths = [9, 30, 14, 16, 28, 14, 8, 10, 30, 28, 14, 8, 9] + [15, 9, 12, 8] * PHONE_SLOTS + [11, 12, 34, 46]
    table(cl, head, flat, widths)

    ph = wb.create_sheet("Phones")
    phead = ["Priority", "Owner Name", "Property Address", "City", "ZIP", "Phone", "Type", "Tier",
             "Trestle Score", "Segment", "Address Note", "DataSift Record"]
    table(ph, phead, phone_rows, [9, 24, 28, 14, 8, 15, 9, 12, 9, 30, 34, 46])

    tmp = base + "_PENDING_.xlsx"
    wb.save(tmp)
    os.replace(tmp, base + ".xlsx")
    with open(base + "_phones.csv", "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=phead, extrasaction="ignore")
        w.writeheader()
        for r in phone_rows:
            w.writerow({k: _clean(r.get(k, "")) for k in phead})
    named = sum(1 for o in owners_out if o["Owner First"] or o["Owner Last"])
    print("export: %s owners, %s properties, %s phone rows (Dial First %s, Dial Second %s); "
          "%s owners with names"
          % (f"{len(owners_out):,}", f"{n_props:,}", f"{len(phone_rows):,}",
             f"{sum(1 for p in phone_rows if p['Tier'] == 'Dial First'):,}",
             f"{sum(1 for p in phone_rows if p['Tier'] == 'Dial Second'):,}", f"{named:,}"))
    print("  %s.xlsx\n  %s_phones.csv" % (base, base))


PHASES = {"probe": phase_probe, "neighborhoods": phase_neighborhoods,
          "count": phase_count, "sweep": phase_sweep, "pull": phase_pull,
          "qa": phase_qa, "phones": phase_phones, "harvest": phase_harvest,
          "score": phase_score, "writeback": phase_writeback, "export": phase_export}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--variant", default="auto", choices=("auto", "ai", "lists"))
    ap.add_argument("--target", default="ty+1@dataflik.com", choices=sorted(TARGETS),
                    help="account the pull writes into (pull/qa phases)")
    ap.add_argument("--sample", type=int, default=800, help="owners to read (phones)")
    ap.add_argument("--seed", type=int, default=29)
    ap.add_argument("--workers", type=int, default=6, help="score: Trestle threads")
    ap.add_argument("--max-spend", type=float, default=1440.0,
                    help="score: refuse above this (Ty approved ~$1,237 all-paid)")
    ap.add_argument("--allow-partial", action="store_true")
    ap.add_argument("--readback", type=int, default=60)
    ap.add_argument("--offline", action="store_true", help="export: skip the live name pull (layout test)")
    args = ap.parse_args()
    PHASES[args.phase](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
