#!/usr/bin/env python
"""Live Doors per Deal: the County List Playbook's ladder for a county, kept current from the Open API.

Ty, 2026-11-24: "as real-time data as we possibly can using the sold property data and the API. That way,
anyone will be able to actually deploy it and use it and get real-time, like, 'These are the best lists'
... Anything that we can't pull in, we can just use the Doors per Deal data itself."

    python src/dpd_live.py --fips 47093                   # today's ladder, best lists first
    python src/dpd_live.py --fips 47093 --fips 47009 --phase all   # + this month's member snapshot, + measured deals
    python src/dpd_live.py --fips 47093 --phase snapshot --force   # take the month's snapshot again

Needs REISIFT_API_KEY (the account's Open API key) and nothing else. Every request is a SiftMap search
(the one path this module posts to cannot add a record), paced under DataSift's per-account limit.
Output in DPD_LIVE_DIR (default output/dpd_live/<fips>/): ladder.json, ladder.csv, snapshots/, sold/.

WHAT IS LIVE, WHAT IS MEASURED, WHAT IS BORROWED. Every number in the output says which.
  DOORS   live. Each rung's list size is a SiftMap count with the rung's own filter, the filter the
          published ladder was built with. An AI rung is live only where the account holds that
          county's off-market AI add-on (a per-county purchase); elsewhere it is the published size,
          scaled by how much the county has moved since.
  SPEED   live. The county's investor purchases by month from the sold-property search, the newest
          month left out (deeds reach the data four to eight weeks after a closing) and any month far
          below the rest dropped (shard_guard). It is compared with the SAME query over the published
          ladder's own months, because the two counting methods differ: SiftMap's labeled sales read
          659 for Knox, January to June 2026, where the ladder's back-test read 927.
  DEALS   measured where we can, borrowed where we cannot. "Sold to an investor and on the list today"
          is wrong in both directions, because the flags describe whoever owns the house after the
          sale (Knox, December to May: tax delinquent 50 back-tested deals against 0 live, absentee
          345 against 467). So deals are measured FORWARD: once a month every rung's members are
          saved (search rows, ids only), and each later month's investor purchases are matched to the
          snapshot taken before that month began. Until a rung has sales of its own, its deals per
          door are the published ladder's, times today's speed; it moves to its measured rate as it
          earns sales (weight sales / (sales + PRIOR_DEALS)).

The ladder is read in the published Work Order (the ladder IS the ranking on the page), each rung's
doors and deals marginal: only what the rungs above it did not already reach. Each rung also carries
its own doors per deal as a list on its own, the answer to "is this list worth marketing".
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import shard_guard  # noqa: E402

MAP = "https://map.reisift.io"
SEARCH_PATH = "/properties/search/"            # the one SiftMap path this module posts to
SHARD_URLS = {"capture": "https://learn.datasift.ai/capture-data/%s.json",
              "county": "https://learn.datasift.ai/county-data/%s.json"}
# The whole-world viewport: a search intersects its addresses with the map view.
WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852}, {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103}, {"lon": -163.30518783395442, "lat": 72.08452694723852}]
STD = {"accept": "application/json", "content-type": "application/json", "origin": "https://beta.reisift.io",
       "referer": "https://beta.reisift.io/"}
BASE = {"type_single_family": True}           # the published ladder's own basis: single family, county wide
INVESTOR_TYPES = ["pending", "wholesale", "wholetail", "rental", "flip"]
K_TYPE = "extra_last_sale_investor_transaction_type"
PAGE = 250
WALL = 10_000                 # SiftMap refuses a page past 10,000 rows
SNAPSHOT_CAP = 10_000         # a rung larger than this is not saved; it keeps the published ratios
PRIOR_DEALS = 8.0             # how many sales of its own a rung needs before its measured rate counts as much as the ladder's
LAG_MONTHS = 1                # the newest whole month is still being recorded
SPEED_MONTHS = 6
FRESH_DAYS = 120              # a month of sales younger than this is read again each run (deeds keep arriving)
SWING = 0.25                  # a list that moved more than this since the published ladder is said
OUT = Path(os.environ.get("DPD_LIVE_DIR") or (ROOT / "output" / "dpd_live"))
WINDOW_RX = re.compile(r"(\d{4})-(\d{2})\s*\.\.\s*(\d{4})-(\d{2})")


class SearchError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------- transport
def county_address(c: dict) -> dict:
    """The county in the shape the search wants. `search` is the bare county name: the full
    'Name County, ST' is accepted and answers zero, which reads exactly like an empty county."""
    title = "%s County, %s" % (c["name"], c["state"])
    return {"state": c["state"], "title": title, "value": title, "county": c["name"], "searchType": "county",
            "counties": [{"fips": str(c["fips"]), "county_name": c["name"]}], "search": c["name"], "type": "county"}


def body(c: dict, filters: dict, offset: int = 0, size: int | None = None) -> dict:
    b = {"result_index": 1 + offset, "with_boundaries": False, "filters": dict(BASE, **filters),
         "addresses": [county_address(c)], "polygon": WORLD}          # result_index is a row offset, not a page
    if size:
        b["size"] = int(size)
    return b


class Search:
    """SiftMap's property search and nothing else: the path is fixed, so this cannot add a record.
    `post` stands in for the network (the brain passes its own budgeted client; tests pass a fake)."""

    def __init__(self, key: str = "", post=None, gap: float = 1.2, cap: int | None = None):
        self.key, self._post, self.gap, self.cap, self.n = key, post, float(gap), cap, 0
        self._last = 0.0

    def __call__(self, b: dict) -> dict:
        if self.cap is not None and self.n >= self.cap:
            raise SearchError("the run's limit of %d searches is used" % self.cap)
        self.n += 1
        if self._post is not None:
            r = self._post(b)
            if not isinstance(r, dict):
                raise SearchError("the search answered something that is not a result")
            return r
        if not self.key:
            raise SearchError("no Open API key (set REISIFT_API_KEY)")
        req = urllib.request.Request(MAP + SEARCH_PATH, json.dumps(b).encode("utf-8"),
                                     headers=dict(STD, authorization="Api-Key " + self.key), method="POST")
        for attempt in range(4):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:200]
                if e.code == 429 and attempt < 3:
                    try:
                        hold = float((e.headers.get("Retry-After") if e.headers else "") or "")
                    except ValueError:
                        hold = 10.0 * (attempt + 1)
                    time.sleep(min(hold, 90))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < 2:
                    time.sleep(3 + 4 * attempt)
                    continue
                raise SearchError("HTTP %s from the SiftMap search: %s" % (e.code, text))
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(3 + 4 * attempt)
                    continue
                raise SearchError("could not reach SiftMap: %s" % e)
        raise SearchError("SiftMap kept refusing")

    def count(self, c: dict, filters: dict) -> int:
        return int(self(body(c, filters, size=1)).get("total_results") or 0)

    def rows(self, c: dict, filters: dict, cap: int = WALL) -> tuple[list, int]:
        """Every row up to `cap` (never past the 10,000-row wall), and the total the search reported."""
        out, offset, total = [], 0, None
        cap = min(int(cap), WALL)
        while True:
            r = self(body(c, filters, offset, PAGE))
            if total is None:
                total = int(r.get("total_results") or 0)
            page = r.get("data") or []
            out += page
            offset += len(page)                 # the next page starts after the rows read, whatever a page holds
            if not page or len(out) >= min(total, cap):
                break
        return out[:cap], total


# ------------------------------------------------------------------------------------- months
def month_label(d: date) -> str:
    return "%04d-%02d" % (d.year, d.month)


def month_range(label: str) -> tuple[date, date]:
    y, m = int(label[:4]), int(label[5:7])
    first = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def shift(label: str, k: int) -> str:
    y, m = int(label[:4]), int(label[5:7]) - 1 + k
    return "%04d-%02d" % (y + m // 12, m % 12 + 1)


def recorded_months(today: date, n: int = SPEED_MONTHS, lag: int = LAG_MONTHS) -> list[str]:
    """The last `n` whole months, skipping the current one and `lag` more (their deeds are still being
    recorded), oldest first."""
    last = shift(month_label(today), -(1 + lag))
    return [shift(last, -i) for i in range(n - 1, -1, -1)]


def window_months(window: str) -> list[str]:
    """'2026-01 .. 2026-06 (6 complete months)' -> ['2026-01', ..., '2026-06']."""
    m = WINDOW_RX.search(window or "")
    if not m:
        return []
    a, b = "%s-%s" % (m.group(1), m.group(2)), "%s-%s" % (m.group(3), m.group(4))
    out, cur = [], a
    while cur <= b and len(out) < 24:
        out.append(cur)
        cur = shift(cur, 1)
    return out


def sold_filters(label: str) -> dict:
    """An investor purchase in one month: the ladder check's own sold-property query."""
    first, last = month_range(label)
    return {K_TYPE: list(INVESTOR_TYPES), "extra_last_sale_value_min": 1000, "extra_is_last_sale_interfamily": False,
            "extra_last_sale_date_min": first.isoformat(), "extra_last_sale_date_max": last.isoformat()}


# ------------------------------------------------------------------------------------- the published ladder
_SHARDS: dict = {}


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "SiftStack dpd_live", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def shard(family: str, st2: str, fetch=None) -> dict:
    key = (family, st2)
    if key not in _SHARDS:
        _SHARDS[key] = (fetch or fetch_json)(SHARD_URLS[family] % st2) or {}
    return _SHARDS[key]


def load_county(fips: str, fetch=None) -> dict:
    """The published ladder (capture-data) and the standalone table (county-data) for one county."""
    fips = str(fips)
    cap = shard("capture", fips[:2], fetch).get(fips)
    cty = shard("county", fips[:2], fetch).get(fips)
    if not cap:
        raise SearchError("the published ladder has no county %s (learn.datasift.ai/capture-data/%s.json)" % (fips, fips[:2]))
    name = (cty or {}).get("name") or cap.get("name") or fips
    name = re.sub(r"\s+County$", "", str(name))
    state = (cty or {}).get("state") or cap.get("st") or ""
    return {"fips": fips, "name": name, "state": state, "capture": cap, "county": cty or {}}


def rung_filters(keys: list, ai=None, ai_cutoffs=()) -> dict:
    """The ladder's own rung filter: absentee is preset_absentee_owners, a preset_* key is itself,
    every other key is a distressor that must ALL match; an AI rung is a band up to the next cutoff."""
    f, dist = {}, []
    for k in keys or []:
        if k == "is_absentee_owners":
            f["preset_absentee_owners"] = True
        elif str(k).startswith("preset_"):
            f[k] = True
        else:
            dist.append(k)
    if dist:
        f["distressors_include"] = ",".join(sorted(dist))
        f["distressors_include_min_match"] = len(dist)
    if ai is not None:
        f["investor_off_market_score_min"] = int(ai)
        higher = [a for a in ai_cutoffs if a > ai]
        if higher:
            f["investor_off_market_score_max"] = int(min(higher)) - 1
    return f


def ladder_rungs(cap: dict, variant: str | None = None) -> tuple[str, list]:
    variants = cap.get("variants") or {}
    if variant is None:
        variant = "ai" if cap.get("ai_provisioned") and variants.get("ai") else "lists"
    rows = variants.get(variant) or []
    cutoffs = sorted({r["ai"] for r in rows if r.get("ai") is not None})
    out = []
    for i, r in enumerate(rows, 1):
        out.append({"n": i, "label": r.get("label") or "rung %d" % i, "keys": list(r.get("keys") or []), "ai": r.get("ai"),
                    "priority": r.get("priority"), "filters": rung_filters(r.get("keys"), r.get("ai"), cutoffs),
                    "shard": {"size": r.get("list_size"), "marg_deals": r.get("marg_deals"), "marg_supply": r.get("marg_supply"),
                              "marg_dpd": r.get("marg_dpd"), "cum_dpd": r.get("cum_dpd"), "est": bool(r.get("est")),
                              "thin": bool(r.get("thin"))}})
    return variant, out


def standalone_rows(cty: dict) -> dict:
    """The county table's lists on their own, by their key set (or 'ai>=T'): deals, size, six months."""
    out = {}
    for r in cty.get("rows") or []:
        if r.get("keys") and r.get("deals") and r.get("list_size"):
            out[frozenset(r["keys"])] = {"deals": float(r["deals"]), "size": float(r["list_size"])}
    for c in (cty.get("ai_demand") or {}).get("cutoffs") or []:
        if c.get("deals") and c.get("list_size"):
            out["ai>=%s" % c.get("cutoff")] = {"deals": float(c["deals"]), "size": float(c["list_size"])}
    return out


# ------------------------------------------------------------------------------------- live counts
def ai_live(search: Search, c: dict, base: int, cutoff: int = 95) -> bool:
    """True when this account sees off-market AI scores in this county (a per-county add-on). Without
    it the AI filter matches nothing; a filter the search ignored would answer the whole county. The
    top cutoff is asked, because nearly every house in a provisioned county carries some score."""
    n = search.count(c, {"investor_off_market_score_min": int(cutoff)})
    return 0 < n < base


def ai_bands_honored(search: Search, c: dict, rungs: list) -> bool:
    """SiftMap ignores a filter key it does not know and answers as if it were not there, so the AI
    band's upper bound is proven before a band is counted or saved with it: the band must equal the
    difference of the two cutoffs."""
    probe = next((r for r in rungs if r["ai"] is not None and "investor_off_market_score_max" in r["filters"]), None)
    if probe is None:
        return True
    lo, hi = probe["filters"]["investor_off_market_score_min"], probe["filters"]["investor_off_market_score_max"]
    band = search.count(c, {"investor_off_market_score_min": lo, "investor_off_market_score_max": hi})
    diff = search.count(c, {"investor_off_market_score_min": lo}) - search.count(c, {"investor_off_market_score_min": hi + 1})
    return abs(band - diff) <= max(2, 0.01 * max(diff, 1))


def live_doors(search: Search, c: dict, rungs: list, base: int, shard_base, ai_ok: bool, bands_ok: bool = True) -> dict:
    """n -> {"doors", "source"}: every rung counted with its own filter; an AI rung borrowed from the
    published ladder where the account has no AI add-on for the county, and counted as the difference
    of two cutoffs where the band's upper bound is not honored."""
    out = {}
    scale = (base / float(shard_base)) if shard_base else 1.0
    for r in rungs:
        if r["ai"] is not None and not ai_ok:
            size = r["shard"]["size"]
            out[r["n"]] = {"doors": int(round(size * scale)) if size else None,
                           "source": "published size (no AI add-on for this county), scaled by the county's change"}
            continue
        if r["ai"] is not None and not bands_ok and "investor_off_market_score_max" in r["filters"]:
            hi = r["filters"]["investor_off_market_score_max"]
            n = (search.count(c, {"investor_off_market_score_min": r["ai"]})
                 - search.count(c, {"investor_off_market_score_min": hi + 1}))
            out[r["n"]] = {"doors": n, "source": "live (two cutoffs)"}
            continue
        out[r["n"]] = {"doors": search.count(c, r["filters"]), "source": "live"}
    return out


# ------------------------------------------------------------------------------------- files
def county_dir(fips: str, root: Path | None = None) -> Path:
    p = Path(root or OUT) / str(fips)
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path, default=None):
    try:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if str(path).endswith(".gz"):
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(obj, fh)
    else:
        tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------- sold, speed
def sold_month(search: Search, c: dict, label: str, folder: Path, today: date, refresh: bool = False) -> dict:
    """{"total", "ids", "at"} for one month of investor purchases, kept on disk. A month younger than
    FRESH_DAYS is read again (its deeds keep arriving); an older one is final."""
    path = folder / "sold" / ("%s.json" % label)
    have = read_json(path)
    first, last = month_range(label)
    fresh = (today - last).days < FRESH_DAYS
    if have and not refresh and not (fresh and have.get("at", "") < today.isoformat()):
        return have
    rows, total = search.rows(c, sold_filters(label), cap=WALL)
    got = {"total": total, "ids": sorted({str(r["id"]) for r in rows if r.get("id")}), "at": today.isoformat(),
           "short": max(0, total - len(rows))}
    write_json(path, got)
    return got


def market_speed(search: Search, c: dict, cap: dict, folder: Path, today: date, months: int = SPEED_MONTHS,
                 lag: int = LAG_MONTHS) -> dict:
    """Today's investor buying against the published ladder's own months, both by the same live query."""
    recent = recorded_months(today, months, lag)
    window = window_months(cap.get("window") or "")
    counts = {}
    for label in sorted(set(recent) | set(window)):
        counts[label] = sold_month(search, c, label, folder, today)["total"]
    verdicts = shard_guard.judge([counts[m] for m in recent], recent)
    kept = [v["label"] for v in verdicts if v["kept"]] or recent
    now_rate = sum(counts[m] for m in kept) / float(len(kept))
    then = [counts[m] for m in window]
    then_rate = (sum(then) / float(len(then))) if then else 0.0
    ratio = (now_rate / then_rate) if then_rate else 1.0
    unit = (float(cap.get("deals") or 0) / sum(then)) if then and sum(then) else 1.0
    return {"ratio": round(ratio, 4), "now_per_month": round(now_rate, 1), "then_per_month": round(then_rate, 1),
            "recent": {m: counts[m] for m in recent}, "window": {m: counts[m] for m in window},
            "kept_line": shard_guard.window_line(verdicts) if verdicts else "", "unit_factor": round(unit, 4),
            "published_deals": cap.get("deals")}


# ------------------------------------------------------------------------------------- snapshots
def snapshot_path(folder: Path, day: date) -> Path:
    return folder / "snapshots" / ("%s.json.gz" % day.isoformat())


def snapshots(folder: Path) -> list[dict]:
    """Every saved snapshot, oldest first."""
    out = []
    for p in sorted((folder / "snapshots").glob("*.json.gz")):
        s = read_json(p)
        if isinstance(s, dict) and s.get("date") and isinstance(s.get("rungs"), dict):
            out.append(s)
    return out


def take_snapshot(search: Search, c: dict, rungs: list, doors: dict, ai_ok: bool, today: date,
                  cap: int = SNAPSHOT_CAP) -> dict:
    """Every member of every rung small enough to page, by its SiftMap id (and its record id when the
    house is in this account). The ids are what a later month's purchases are matched against."""
    snap = {"date": today.isoformat(), "fips": c["fips"], "rungs": {}, "skipped": {}, "saved": {}, "order": [r["label"] for r in rungs]}
    for r in rungs:
        d = (doors.get(r["n"]) or {}).get("doors")
        if r["ai"] is not None and not ai_ok:
            snap["skipped"][r["label"]] = "its AI band cannot be searched with this account"
            continue
        if d is None or d > cap:
            snap["skipped"][r["label"]] = "too large to save (%s houses)" % ("unknown" if d is None else format(d, ","))
            continue
        rows, total = search.rows(c, r["filters"], cap=cap)
        snap["rungs"][r["label"]] = sorted({str(x["id"]) for x in rows if x.get("id")})
        for x in rows:
            if x.get("id") and x.get("saved_uuid"):
                snap["saved"][str(x["id"])] = str(x["saved_uuid"])
    return snap


def marginal_sets(snap: dict, order: list[str]) -> tuple[dict, dict]:
    """label -> the members no rung above it holds, and label -> whether that is exact (every rung above
    it was saved). A rung below one too large to save reads larger than it is."""
    seen, out, exact, whole = set(), {}, {}, True
    for label in order:
        if label not in snap["rungs"]:
            whole = False
            continue
        members = set(snap["rungs"][label])
        out[label] = members - seen
        exact[label] = whole
        seen |= members
    return out, exact


def measure(snaps: list[dict], sold: dict, order: list[str], months: list[str]) -> dict:
    """label -> {"months", "deals", "door_months", "marg_deals", "marg_door_months"}: each month's
    purchases matched to the newest snapshot taken BEFORE that month began. A month with no earlier
    snapshot is not measured; that is what keeps a house that joined a list after its sale out."""
    out: dict = {}
    for label in months:
        got = sold.get(label)
        if not got:
            continue
        first, _ = month_range(label)
        before = [s for s in snaps if s["date"] < first.isoformat()]
        if not before:
            continue
        snap = before[-1]
        ids = set(got.get("ids") or [])
        marg, _exact = marginal_sets(snap, order)
        for name, members in snap["rungs"].items():
            m = out.setdefault(name, {"months": 0, "deals": 0, "door_months": 0, "marg_deals": 0, "marg_door_months": 0,
                                      "from": label})
            m["months"] += 1
            m["deals"] += len(ids & set(members))
            m["door_months"] += len(members)
            m["marg_deals"] += len(ids & marg.get(name, set()))
            m["marg_door_months"] += len(marg.get(name, set()))
    return out


# ------------------------------------------------------------------------------------- the estimate
def _rate(deals, doors) -> float | None:
    try:
        return float(deals) / float(doors) if deals is not None and doors else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def blend(prior: float | None, measured: float | None, sales: float) -> tuple[float | None, str]:
    """A rung's six-month deals per door: the published rate at today's speed, moving to its own measured
    rate as it earns sales of its own."""
    if measured is None or sales <= 0:
        return prior, "published ladder at today's speed"
    if prior is None:
        return measured, "measured (%d sale%s)" % (sales, "" if sales == 1 else "s")
    w = sales / (sales + PRIOR_DEALS)
    word = "measured (%d sale%s)" % (sales, "" if sales == 1 else "s") if w >= 0.5 else \
        "published ladder, moving to measured (%d sale%s)" % (sales, "" if sales == 1 else "s")
    return w * measured + (1 - w) * prior, word


def estimate(county: dict, rungs: list, doors: dict, base: int, speed: dict, measured: dict | None = None,
             snap: dict | None = None) -> dict:
    """The live ladder: per rung, doors now, the marginal doors and deals it adds in six months, its
    doors per deal on the margin and as a list on its own, each with where it came from."""
    cap, cty = county["capture"], county["county"]
    ratio = float(speed.get("ratio") or 1.0)
    unit = float(speed.get("unit_factor") or 1.0)
    alone = standalone_rows(cty)
    order = [r["label"] for r in rungs]
    marg_live, exact = marginal_sets(snap, order) if snap else ({}, {})
    measured = measured or {}
    rows, cum_doors, cum_deals = [], 0.0, 0.0
    for r in rungs:
        s, d = r["shard"], doors.get(r["n"]) or {}
        now = d.get("doors")
        size, msup = s.get("size"), s.get("marg_supply")
        if label_in(snap, r["label"]):
            marg_doors, marg_src = len(marg_live[r["label"]]), "saved members" + ("" if exact.get(r["label"]) else ", approximate")
        elif now is not None and size and msup is not None:
            marg_doors, marg_src = now * (float(msup) / float(size)), "today's size at the published share"
        else:
            marg_doors, marg_src = msup, "published"
        m = measured.get(r["label"]) or {}
        prior_m = _rate(s.get("marg_deals"), msup)
        prior_m = prior_m * ratio if prior_m is not None else None
        meas_m = _rate(m.get("marg_deals", 0) * unit * 6.0, m.get("marg_door_months")) if m.get("marg_door_months") else None
        rate_m, src = blend(prior_m, meas_m, float(m.get("marg_deals") or 0))
        st = alone.get(frozenset(r["keys"])) if r["ai"] is None else None     # an AI band has no table row of its own
        prior_s = _rate(st["deals"], st["size"]) * ratio if st else None
        meas_s = _rate(m.get("deals", 0) * unit * 6.0, m.get("door_months")) if m.get("door_months") else None
        rate_s, src_s = blend(prior_s, meas_s, float(m.get("deals") or 0))
        deals = (rate_m * marg_doors) if (rate_m is not None and marg_doors is not None) else None
        if marg_doors is not None:
            cum_doors += float(marg_doors)
        if deals is not None:
            cum_deals += deals
        swing = None
        if now is not None and size:
            swing = round(now / float(size) - 1.0, 3)
        rows.append({
            "n": r["n"], "label": r["label"], "priority": r["priority"], "keys": r["keys"], "ai": r["ai"],
            "doors": now, "doors_source": d.get("source", ""), "doors_published": size, "swing": swing,
            "marg_doors": None if marg_doors is None else int(round(marg_doors)), "marg_doors_source": marg_src,
            "deals_6mo": None if deals is None else round(deals, 2),
            "dpd": round(1.0 / rate_m, 1) if rate_m else None, "dpd_published": s.get("marg_dpd"), "dpd_source": src,
            "dpd_alone": round(1.0 / rate_s, 1) if rate_s else None, "dpd_alone_source": src_s if rate_s else "",
            "cum_dpd": round(cum_doors / cum_deals, 1) if cum_deals else None,
            "rate": rate_m, "rate_published": _rate(s.get("marg_deals"), msup),
            "measured": m or None, "thin": s.get("thin"), "est": s.get("est"),
        })
    pub_deals = float(cap.get("deals") or 0)
    base_rate = (pub_deals * ratio / float(base)) if base and pub_deals else None
    return {"rungs": rows, "baseline_dpd": round(1.0 / base_rate, 1) if base_rate else None,
            "baseline_dpd_published": round(float(cap.get("base_supply") or 0) / pub_deals, 1) if pub_deals else None}


def label_in(snap: dict | None, label: str) -> bool:
    return bool(snap) and label in (snap.get("rungs") or {})


def best_lists(ladder: dict, n: int = 10) -> list[dict]:
    """The lists to work first: each rung on its own where the county table has it, else on the
    margin, lowest doors per deal first."""
    rows = [r for r in ladder["rungs"] if (r.get("dpd_alone") or r.get("dpd"))]
    return sorted(rows, key=lambda r: (r.get("dpd_alone") or r.get("dpd")))[:n]


# ------------------------------------------------------------------------------------- one county
def run_county(search: Search, fips: str, root: Path | None = None, today: date | None = None, phase: str = "ladder",
               force_snapshot: bool = False, fetch=None, variant: str | None = None) -> dict:
    """One county, start to finish. phase: ladder (doors, speed, the estimate), snapshot (+ this month's
    members, when not yet saved), all (+ the measured deals). Writes ladder.json and ladder.csv."""
    today = today or datetime.now(timezone.utc).date()
    county = load_county(fips, fetch)
    c = {"fips": county["fips"], "name": county["name"], "state": county["state"]}
    folder = county_dir(c["fips"], root)
    variant, rungs = ladder_rungs(county["capture"], variant)
    notes = []
    base = search.count(c, {})
    if not base:
        raise SearchError("%s, %s answers no single-family houses: check the county's name and state" % (c["name"], c["state"]))
    ai_cuts = [r["ai"] for r in rungs if r["ai"] is not None]
    ai_ok = ai_live(search, c, base, max(ai_cuts)) if ai_cuts else False
    if any(r["ai"] is not None for r in rungs) and not ai_ok:
        notes.append("This account has no off-market AI add-on for %s, so the AI rungs are the published sizes." % c["name"])
    bands_ok = ai_bands_honored(search, c, rungs) if ai_ok else True
    if ai_ok and not bands_ok:
        notes.append("SiftMap did not honor the AI score's upper bound, so each AI rung was counted as two cutoffs "
                     "and its members were not saved.")
    doors = live_doors(search, c, rungs, base, county["capture"].get("base_supply"), ai_ok, bands_ok)
    speed = market_speed(search, c, county["capture"], folder, today)
    snaps = snapshots(folder)
    month = month_label(today)
    have_month = any(s["date"][:7] == month for s in snaps)
    if phase in ("snapshot", "all") and (force_snapshot or not have_month):
        snap = take_snapshot(search, c, rungs, doors, ai_ok and bands_ok, today)
        write_json(snapshot_path(folder, today), snap)
        snaps = snapshots(folder)
    measured = {}
    if phase == "all" and snaps:
        start = snaps[0]["date"][:7]
        months = [m for m in recorded_months(today, 24, LAG_MONTHS) if m > start]
        sold = {m: sold_month(search, c, m, folder, today) for m in months}
        measured = measure(snaps, sold, [r["label"] for r in rungs], months)
    elif snaps:
        prev = read_json(folder / "ladder.json") or {}
        measured = prev.get("measured") or {}
    latest = snaps[-1] if snaps else None
    ladder = estimate(county, rungs, doors, base, speed, measured, latest)
    moved = [r for r in ladder["rungs"] if r["swing"] is not None and abs(r["swing"]) >= SWING and r["doors_source"] == "live"]
    result = {"fips": c["fips"], "name": c["name"], "state": c["state"], "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "today": today.isoformat(), "variant": variant, "ai_live": ai_ok, "base": {"live": base, "published": county["capture"].get("base_supply")},
              "published": {"built": county["capture"].get("built"), "window": county["capture"].get("window"), "deals": county["capture"].get("deals")},
              "speed": speed, "snapshot": ({"date": latest["date"], "rungs": len(latest["rungs"]),
                                            "houses": sum(len(v) for v in latest["rungs"].values()),
                                            "skipped": latest.get("skipped") or {}} if latest else None),
              "measured": measured, "measured_rungs": sum(1 for v in measured.values() if v.get("deals")),
              "moved": [{"label": r["label"], "swing": r["swing"], "doors": r["doors"], "published": r["doors_published"]} for r in moved],
              "notes": notes, "searches": search.n, **ladder}
    write_json(folder / "ladder.json", result)
    write_csv(folder / "ladder.csv", result)
    return result


def write_csv(path: Path, res: dict) -> None:
    cols = ["n", "label", "priority", "doors", "doors_published", "swing", "marg_doors", "deals_6mo", "dpd", "dpd_published",
            "dpd_source", "dpd_alone", "dpd_alone_source", "cum_dpd", "doors_source"]
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in res["rungs"]:
            w.writerow([r.get(k) if r.get(k) is not None else "" for k in cols])
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------- words
def _n(v) -> str:
    return "-" if v is None else format(int(round(v)), ",")


def _pct(x) -> str:
    return "" if x is None else ("%+.0f%%" % (100 * x))


def summary_lines(res: dict, top: int = 5) -> list[str]:
    """A few plain lines for a person: the speed, the lists to work first, what moved."""
    sp = res["speed"]
    out = ["%s: investors bought %s houses a month lately against %s in the published ladder's months (%s)." % (
        res["name"], ("%g" % sp["now_per_month"]), ("%g" % sp["then_per_month"]),
        "about the same" if abs(sp["ratio"] - 1) < 0.05 else "%.0f%% %s" % (abs(sp["ratio"] - 1) * 100, "faster" if sp["ratio"] > 1 else "slower"))]
    best = best_lists(res, top)
    if best:
        out.append("%s, best lists now: %s." % (res["name"], "; ".join(
            "%s 1 in %s" % (r["label"], _n(r.get("dpd_alone") or r.get("dpd"))) for r in best)))
    if res.get("moved"):
        out.append("%s, lists that moved since the published ladder: %s." % (res["name"], "; ".join(
            "%s %s to %s (%s)" % (m["label"], _n(m["published"]), _n(m["doors"]), _pct(m["swing"])) for m in res["moved"][:5])))
    if res.get("snapshot"):
        sn = res["snapshot"]
        out.append("%s: members saved %s (%s houses on %d lists); %d list%s with sales of their own so far." % (
            res["name"], sn["date"], _n(sn["houses"]), sn["rungs"], res.get("measured_rungs") or 0,
            "" if (res.get("measured_rungs") or 0) == 1 else "s"))
    return out


def table(res: dict) -> str:
    head = "%-3s %-46s %3s %8s %8s %7s %8s %8s %8s  %s" % ("#", "list (published order)", "pri", "doors", "was", "moved", "dpd", "was", "alone", "source")
    lines = [head, "-" * len(head)]
    for r in res["rungs"]:
        lines.append("%-3s %-46s %3s %8s %8s %7s %8s %8s %8s  %s" % (
            r["n"], r["label"][:46], r.get("priority") or "", _n(r["doors"]), _n(r["doors_published"]), _pct(r["swing"]),
            "-" if r["dpd"] is None else "%.1f" % r["dpd"], "-" if r["dpd_published"] is None else "%.1f" % r["dpd_published"],
            "-" if r["dpd_alone"] is None else "%.1f" % r["dpd_alone"], r["dpd_source"]))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------- CLI
def api_key() -> str:
    k = os.environ.get("REISIFT_API_KEY", "").strip()
    if k:
        return k
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("REISIFT_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live Doors per Deal for a county, from the Open API")
    ap.add_argument("--fips", action="append", required=True, help="a county's 5-digit FIPS (repeat for more)")
    ap.add_argument("--phase", choices=["ladder", "snapshot", "all"], default="ladder")
    ap.add_argument("--force", action="store_true", help="take this month's snapshot again")
    ap.add_argument("--variant", choices=["ai", "lists"], help="which published ladder (default: ai where the county has one)")
    ap.add_argument("--max-searches", type=int, default=2000)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--table", action="store_true", help="print every rung")
    a = ap.parse_args(argv)
    search = Search(key=api_key(), cap=a.max_searches)
    for fips in a.fips:
        res = run_county(search, fips, phase=a.phase, force_snapshot=a.force, variant=a.variant)
        for line in summary_lines(res, a.top):
            print(line)
        for note in res.get("notes") or []:
            print("  note: " + note)
        if a.table:
            print(table(res))
        print("  wrote %s (%d searches so far)" % (county_dir(fips) / "ladder.json", search.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
