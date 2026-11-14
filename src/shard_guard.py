"""Refuse a bad month in the doors-per-deal data before anything ranks on it (build plan 5.1).

The County List Playbook shards (learn.datasift.ai/county-data/<st>.json) label themselves
"6 complete months" from a hard-coded window. Deeds reach the data four to eight weeks after a
closing, so a file built soon after a month ends carries that month as zero. Measured 2026-08-31:
the last month read zero in every county of three state files, and every doors-per-deal figure
read off them sat on five months while claiming six, about 20% weak. Nothing failed: a zero month
looks exactly like a quiet one.

This module is the reader's side of the fix. It never fills a month in. It decides which months
cannot be trusted, drops them, says which and why, and reports the window it used:

    5 complete months, June left out: it reads zero

Two tests decide a month, and both must agree before it is dropped:

  * FAR BELOW: the month reads under FAR_BELOW (35%) of the median of the other months.
  * NOT CHANCE: a count that low would turn up less than once in a thousand months if deals
    arrived at that median rate (a Poisson tail). Without this second test a thin county, where
    one deal a month is normal and a zero month is ordinary, would lose half its window, and
    dropping real quiet months makes every list look better than it is.

A thin county cannot prove a gap on its own numbers, so when the whole state file is in hand the
same two tests are run on the state's monthly totals first. A month the state file is missing is
dropped in every county of it, thin or not.

What a reader gets back (the input is never changed):

    county["guard"]        what was checked: each month, kept or left out, and why
    county["window_used"]  the honest window, in words
    row["dpd_6mo"]         the same doors per deal at a six-month rate (the kept months' pace
                           times six), beside the published figure, never in place of it
    row["median_trusted"]  False when the row's median spread rests on fewer than 15 sales

The published figures stay where they were. `restate=True` (or SHARD_GUARD_RESTATE=1) is the named
switch that moves the six-month figures into `dpd` and `deals` for a reader that ranks on them,
keeping the published ones as `dpd_published` and `deals_published`.

    python src/shard_guard.py <shard.json> [--fips 47093]      # reads a local file, prints the lines
"""
from __future__ import annotations

import copy
import json
import math
import os
import statistics
import sys

FAR_BELOW = 0.35            # a month under this share of the months around it is a candidate
ALPHA = 0.001               # and is dropped only when chance explains it less than 1 time in 1,000
MIN_MONTHS = 3              # fewer complete months than this cannot carry a ranking
MIN_DEALS_FOR_MEDIAN = 15   # a median spread on fewer sales than this is not trusted (15 to 30 are taught)
RESTATE_ENV = "SHARD_GUARD_RESTATE"

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


# --------------------------------------------------------------------------------------- the tests
def chance_of_at_most(count: int, rate: float) -> float:
    """How often a month would read `count` or fewer when deals arrive at `rate` a month."""
    if rate <= 0:
        return 1.0
    if count < 0:
        return 0.0
    term = math.exp(-rate)          # underflows to 0.0 for a very busy county, which is the right answer
    total = term
    for k in range(1, int(count) + 1):
        term *= rate / k
        total += term
    return min(1.0, total)


def _numbers(trend) -> list | None:
    if not isinstance(trend, (list, tuple)) or not trend:
        return None
    out = []
    for v in trend:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            return None
        out.append(v)
    return out


def _g(v) -> str:
    """A count for a sentence: 155, or 4.5, never 155.0."""
    return ("%.1f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def month_labels(window, n: int, fallback=None) -> list[str]:
    """One label per month of the trend, read from a window such as '2026-01 .. 2026-06 (6 complete
    months)'. When the window cannot be read, or names a different number of months than the trend
    holds, the months are numbered instead: a guessed name is worse than none."""
    found = []
    for tok in str(window or "").replace("(", " ").replace(")", " ").replace(",", " ").split():
        if len(tok) == 7 and tok[4] == "-" and tok[:4].isdigit() and tok[5:].isdigit() and 1 <= int(tok[5:]) <= 12:
            found.append((int(tok[:4]), int(tok[5:])))
    if len(found) >= 2:
        (y, m), end = found[0], found[1]
        out = []
        while (y, m) <= end and len(out) <= n:
            out.append("%04d-%02d" % (y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if len(out) == n:
            return out
    if isinstance(fallback, (list, tuple)) and len(fallback) == n and all(str(x).strip() for x in fallback):
        return [str(x) for x in fallback]
    return ["month %d" % (i + 1) for i in range(n)]


def month_name(label: str, with_year: bool = False) -> str:
    s = str(label)
    if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit() and 1 <= int(s[5:]) <= 12:
        name = MONTHS[int(s[5:]) - 1]
        return "%s %s" % (name, s[:4]) if with_year else name
    return s


def judge(trend, labels=None, *, far_below: float = FAR_BELOW, alpha: float = ALPHA) -> list[dict]:
    """One verdict per month: kept, or left out with the reason. An empty list means the trend
    could not be read, and nothing was checked."""
    nums = _numbers(trend)
    if nums is None:
        return []
    n = len(nums)
    labels = list(labels) if labels and len(labels) == n else ["month %d" % (i + 1) for i in range(n)]
    years = {str(x)[:4] for x in labels}
    out = []
    for i, c in enumerate(nums):
        others = nums[:i] + nums[i + 1:]
        around = statistics.median(others) if others else 0
        why = ""
        if around > 0 and c < far_below * around and chance_of_at_most(int(c), float(around)) < alpha:
            why = "it reads zero" if c == 0 else "it reads %s, far below the %s a month around it" % (_g(c), _g(around))
        out.append({"label": labels[i], "name": month_name(labels[i], with_year=len(years) > 1), "deals": c,
                    "kept": not why, "why": why})
    return out


def state_verdict(shard: dict, *, far_below: float = FAR_BELOW, alpha: float = ALPHA) -> dict:
    """The same two tests on the whole state file's monthly totals. A county with one deal a month
    cannot show that a month is missing; four thousand deals a month can."""
    series, windows = [], []
    for c in (shard or {}).values():
        if not isinstance(c, dict):
            continue
        nums = _numbers(c.get("trend"))
        if nums is not None:
            series.append(nums)
            windows.append(str(c.get("window") or ""))
    if not series:
        return {"checked": False, "counties": 0, "months": []}
    n = statistics.mode([len(s) for s in series])
    window = statistics.mode([w for s, w in zip(series, windows) if len(s) == n])
    use = [s for s, w in zip(series, windows) if len(s) == n and w == window]
    totals = [sum(s[i] for s in use) for i in range(n)]
    verdicts = judge(totals, month_labels(window, n), far_below=far_below, alpha=alpha)
    for i, v in enumerate(verdicts):
        v["counties_at_zero"] = sum(1 for s in use if s[i] == 0)
    return {"checked": True, "counties": len(use), "window": window, "months": verdicts}


# ------------------------------------------------------------------------------------ the guard
def _factor(months: list[dict]) -> tuple[float, float, float]:
    """(kept deals, all deals, what a published six-month deal count is multiplied by to read at a
    six-month rate). Nothing is filled in: the kept months' own pace is carried across the window."""
    total = sum(m["deals"] for m in months)
    kept = [m for m in months if m["kept"]]
    used = sum(m["deals"] for m in kept)
    if not kept or total <= 0 or used <= 0:
        return used, total, 1.0
    return used, total, (used / total) * (len(months) / len(kept))


def _count_at(x: float):
    """A deal count carried to a six-month rate: a whole number once it is 10 or more, one decimal
    below that, where rounding 3.6 to 4 would be a tenth of the figure."""
    return int(round(x)) if x >= 10 else round(x, 1)


def window_line(months: list[dict]) -> str:
    """The window in words: '6 complete months' or '5 complete months, June left out: it reads zero'."""
    kept = sum(1 for m in months if m["kept"])
    out = [m for m in months if not m["kept"]]
    head = "%d complete month%s" % (kept, "" if kept == 1 else "s")
    if not out:
        return head
    if len({m["why"] for m in out}) == 1:
        names = [m["name"] for m in out]
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        why = out[0]["why"] if len(out) == 1 else out[0]["why"].replace("it reads", "each reads", 1)
        return "%s, %s left out: %s" % (head, who, why)
    return "%s, %s" % (head, "; ".join("%s left out: %s" % (m["name"], m["why"]) for m in out))


def guard_county(county: dict, *, state: dict | None = None, restate: bool | None = None,
                 far_below: float = FAR_BELOW, alpha: float = ALPHA) -> dict:
    """One county's data with the guard's findings added. The dict handed in is not changed."""
    if restate is None:
        restate = os.environ.get(RESTATE_ENV, "").strip() == "1"
    out = copy.deepcopy(county)
    nums = _numbers(out.get("trend"))
    if nums is None:
        out["guard"] = {"checked": False, "line": "no monthly counts in this county's data, so the window was not checked",
                        "left_out": [], "usable": True, "restated": False}
        return out
    labels = month_labels(out.get("window"), len(nums), (out.get("zip_meta") or {}).get("month_labels"))
    months = judge(nums, labels, far_below=far_below, alpha=alpha)
    state_months = (state or {}).get("months") or []
    if (state or {}).get("checked") and len(state_months) == len(months) and str(state.get("window")) == str(out.get("window") or ""):
        for m, s in zip(months, state_months):
            if m["kept"] and not s["kept"]:
                m["kept"] = False
                if m["deals"] == 0:
                    m["why"] = "it reads zero"
                else:
                    m["why"] = "it reads %s here and the whole state file is short that month (%s)" % (
                        _g(m["deals"]), s["why"].replace("it reads ", "", 1))
    used, total, factor = _factor(months)
    kept = sum(1 for m in months if m["kept"])
    usable = kept >= MIN_MONTHS
    line = window_line(months)
    if not usable:
        line += ". Too few complete months are left to rank on"
    left = [m for m in months if not m["kept"]]
    if not usable or not left:
        factor = 1.0        # nothing dropped, or too little left to project from: the figures stand as published
    g = {"checked": True, "months": months, "left_out": [m["name"] for m in left],
         "months_claimed": len(months), "months_used": kept, "deals_counted": total, "deals_used": used,
         "six_month_factor": round(factor, 4), "usable": usable, "line": line, "restated": False}
    out["guard"] = g
    if left:
        span = "%s .. %s" % (labels[0], labels[-1]) if labels and labels[0][:5] != "month" else "the published window"
        out["window_used"] = "%s (%s)" % (span, line)
    for holder in [out, out.get("baseline")] + list(out.get("rows") or []):
        if not isinstance(holder, dict):
            continue
        deals, dpd = holder.get("deals"), holder.get("dpd")
        if isinstance(deals, (int, float)) and not isinstance(deals, bool):
            holder["deals_6mo"] = _count_at(deals * factor) if factor != 1.0 else deals
            if holder is not out and holder is not out.get("baseline"):
                # A median rests on the sales that were actually counted, not on a pace carried forward.
                holder["median_trusted"] = deals >= MIN_DEALS_FOR_MEDIAN
        if isinstance(dpd, (int, float)) and not isinstance(dpd, bool):
            holder["dpd_6mo"] = round(dpd / factor, 1) if factor != 1.0 else dpd
    if restate and left and usable:
        for holder in [out, out.get("baseline")] + list(out.get("rows") or []):
            if not isinstance(holder, dict):
                continue
            for key in ("deals", "dpd"):
                if key + "_6mo" in holder:
                    holder[key + "_published"] = holder[key]
                    holder[key] = holder[key + "_6mo"]
        out["window_published"] = out.get("window")
        out["window"] = out["window_used"]
        g["restated"] = True
    return out


def county_from(shard: dict, fips: str, *, restate: bool | None = None, far_below: float = FAR_BELOW,
                alpha: float = ALPHA) -> dict | None:
    """The guarded county out of a whole state file, judged against the state's own totals first.
    None when the file holds no such county, exactly as `shard.get(fips)` would answer."""
    county = (shard or {}).get(fips)
    if not isinstance(county, dict):
        return None
    return guard_county(county, state=state_verdict(shard, far_below=far_below, alpha=alpha), restate=restate,
                        far_below=far_below, alpha=alpha)


def lines(county: dict) -> list[str]:
    """What a reader prints under the county line. Empty when the guard dropped nothing."""
    g = (county or {}).get("guard") or {}
    if not g.get("checked"):
        return [g["line"][0].upper() + g["line"][1:] + "."] if g.get("line") else []
    if not g.get("left_out"):
        return []
    out = ["Window used: %s." % g["line"]]
    base = (county.get("baseline") or {})
    if not g.get("usable"):
        out.append("The figures below are as published and should not be ranked on.")
    elif g.get("restated"):
        out.append("Doors per deal and deals below are at a six-month rate (the kept months' pace). Published: "
                   "%s deals, baseline %s doors per deal." % (_g(county.get("deals_published")), _g(base.get("dpd_published"))))
    else:
        out.append("Doors per deal below are as published, on %d months. At a six-month rate: %s deals, baseline %s "
                   "doors per deal (each list's own figure is in dpd_6mo)."
                   % (g["months_used"], _g(county.get("deals_6mo")), _g(base.get("dpd_6mo"))))
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print("usage: python src/shard_guard.py <shard.json> [--fips FIPS]")
        return 2
    fips = args[args.index("--fips") + 1] if "--fips" in args and args.index("--fips") + 1 < len(args) else ""
    with open(args[0], encoding="utf-8") as f:
        shard = json.load(f)
    state = state_verdict(shard)
    if state.get("checked"):
        print("state file: %d counties, %s" % (state["counties"], window_line(state["months"])))
    dropped = 0
    for key in sorted(shard):
        if fips and key != fips:
            continue
        c = county_from(shard, key)
        if c is None:
            continue
        g = c["guard"]
        if g.get("left_out") or fips:
            dropped += 1 if g.get("left_out") else 0
            print("%s %s: %s" % (key, c.get("name") or "", g["line"]))
    if not fips:
        print("%d of %d counties had a month left out" % (dropped, len(shard)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
