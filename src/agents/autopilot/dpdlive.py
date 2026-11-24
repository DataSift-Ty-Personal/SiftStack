"""Live Doors per Deal on the brain: each county's ladder kept current every night from the Open API
(src/dpd_live.py), so the team and the Top 1,000 read the best lists as they are now, not as they were
when the published ladder was built.

Ty, 2026-11-24: "as real-time data as we possibly can using the sold property data and the API ... Then
pair that with the framework to use for the top 1,000. Anything that we can't pull in, we can just use
the Doors per Deal data itself."

Each run, per county in the block:
  * the doors: one SiftMap count per rung, the rung's own filter (AI rungs only where the account holds
    that county's off-market AI add-on; elsewhere the published size);
  * the speed: investor purchases a month from the sold-property search against the published ladder's
    own months, both by the same query (old months are kept on disk, so a night reads the newest few);
  * the first run of a month saves every rung's members (ids), and every run matches the months since the
    first save against the save made before each month began: deals measured forward, the only honest
    way to count a list's sales when a sold house's flags describe its new owner;
  * a rung with no sales of its own keeps the published deals per door at today's speed, and moves to its
    measured rate as it earns sales.

Writes DATA_DIR/dpd_live/<fips>/ (ladder.json, snapshots, sold months), which the Top 1,000 reads when
its block sets live_lists. Nothing is written to the account: the only path this stage posts to is the
SiftMap search. A plan run does the same work, since the files are the stage's own knowledge.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import dpd_live  # noqa: E402

from .. import config, store  # noqa: E402
from . import laddercheck  # noqa: E402
from .stages import Ctx  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_MAX_SEARCHES = 1200      # a month's first run saves every rung's members: about 430 searches for Knox and Blount


def root() -> Path:
    return Path(config.DATA_DIR) / "dpd_live"


def block_problems(st) -> list[str]:
    out = []
    counties = st.get("counties")
    if not isinstance(counties, list) or not counties:
        out.append("stage %s: name the counties, as counties: [{fips, name, state}]" % st.id)
    elif any(not isinstance(c, dict) or not str(c.get("fips") or "").isdigit() or len(str(c.get("fips"))) != 5
             for c in counties):
        out.append("stage %s: every county needs its 5-digit fips" % st.id)
    ms = st.get("max_searches")
    if ms is not None and (not isinstance(ms, int) or ms < 50):
        out.append("stage %s: max_searches must be a whole number of 50 or more" % st.id)
    return out


def run_dpdlive(ctx: Ctx) -> None:
    st = ctx.stage
    counties = [c for c in (st.get("counties") or []) if isinstance(c, dict) and c.get("fips")]
    if not counties:
        ctx.out.problem("no county is named in the playbook (counties: [{fips, name, state}])")
        ctx.out.head = "Best lists: no county to read."
        return
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    search = dpd_live.Search(post=lambda b: laddercheck._map_search(ctx.crm, b),
                             cap=int(st.get("max_searches") or DEFAULT_MAX_SEARCHES))
    top = int(st.get("top") or 5)
    done, kept = [], {}
    for c in counties:
        fips = str(c["fips"])
        try:
            res = dpd_live.run_county(search, fips, root=root(), today=today, phase="all")
        except Exception as exc:  # noqa: BLE001  (one county failing must not hide the other)
            ctx.out.problem("%s: the live ladder could not be built (%s)" % (c.get("name") or fips, str(exc)[:200]))
            continue
        done.append(res["name"])
        for line in dpd_live.summary_lines(res, top):
            ctx.out.line(line)
        for note in res.get("notes") or []:
            ctx.out.line("%s: %s" % (res["name"], note))
        best = dpd_live.best_lists(res, top)
        kept[fips] = {"name": res["name"], "built": res["built"], "speed": res["speed"]["ratio"],
                      "baseline_dpd": res["baseline_dpd"], "measured_rungs": res.get("measured_rungs") or 0,
                      "snapshot": (res.get("snapshot") or {}).get("date"),
                      "best": [{"label": r["label"], "dpd": r.get("dpd_alone") or r.get("dpd"), "doors": r["doors"]} for r in best],
                      "moved": res.get("moved") or []}
        ctx.out.tally["lists read in %s" % res["name"]] = len(res["rungs"])
    store.kv_set("autopilot:dpd_live", json.dumps(kept, default=str))      # the autopilot's own record
    ctx.out.found = sum(len(v["best"]) for v in kept.values())
    ctx.out.tally["SiftMap searches"] = search.n
    where = done[0] if len(done) == 1 else (", ".join(done[:-1]) + " and " + done[-1]) if done else "no county"
    measured = sum(v["measured_rungs"] for v in kept.values())
    ctx.out.head = "Best lists for %s, live from the sold-property data%s." % (
        where, (": %d list%s already measured on their own sales" % (measured, "" if measured == 1 else "s")) if measured
        else ": the published ladder at today's speed until the lists earn sales of their own")


# ---------------------------------------------------------------------- what the Top 1,000 reads
def list_factors(fips_list: list[str], base: Path | None = None) -> dict[str, tuple[float, str]]:
    """dataflik id -> (how much better or worse the house's list sells now than the published ladder says,
    the list's name), from each county's latest ladder and saved members. A house sits on the first rung
    of the published order whose members hold it. 1.0 until a list has measured sales of its own: the
    market's speed moves every list alike, so it never reorders houses on its own."""
    out: dict[str, tuple[float, str]] = {}
    for fips in fips_list:
        folder = Path(base or root()) / str(fips)
        ladder = dpd_live.read_json(folder / "ladder.json") or {}
        snaps = dpd_live.snapshots(folder)
        if not ladder.get("rungs") or not snaps:
            continue
        ratio = float((ladder.get("speed") or {}).get("ratio") or 1.0)
        factor = {}
        for r in ladder["rungs"]:
            pub, now = r.get("rate_published"), r.get("rate")
            factor[r["label"]] = (now / (pub * ratio)) if (pub and now and ratio) else 1.0
        snap = snaps[-1]
        for label in snap.get("order") or [r["label"] for r in ladder["rungs"]]:
            for i in snap["rungs"].get(label) or []:
                if i not in out:
                    out[i] = (round(factor.get(label, 1.0), 4), label)
    return out
