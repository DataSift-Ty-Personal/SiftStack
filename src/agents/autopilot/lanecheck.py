"""The nightly owner-lane check (build plan 0.13): one owner, one lane.

Reads every record that sits in a lane (leads by status, the other lanes by their tags and lists),
groups them by owner, and remembers for each owner the highest lane any of their records is in.
The gate then holds an owner's lower-lane records, and this stage lists the owners who today sit
in two lanes at once, which is the number the preset patch is meant to drive to zero.

The map is the autopilot's own memory of the account, not a change to it, so it is refreshed in
plan mode as well: a plan that ignored it would promise touches the gate will refuse.

Bulk is not scanned. It is the lane a record is in when nothing higher claims it, so an owner who
appears nowhere in the map is a bulk owner by definition.

A search stops at 10,000 rows. A lane larger than that (Priority 2 holds about 20,000 records in
ty+2) is read in slices by the date the record was created, halving a slice until it fits.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, timedelta

from . import lanes
from .stages import Ctx, addr

log = logging.getLogger(__name__)

SLICE_MAX = 9500
FIRST_DAY = date(2015, 1, 1)


def lane_musts(ctx: Ctx, lane: str) -> list[dict]:
    """The searches that together hold a lane's records. Finished records hold no lane."""
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    gone = {"any_property_status": list(lanes.GONE_STATUS_VALUES)}
    if lane == "lead":
        return [{"any_property_status": list(lanes.LEAD_STATUS_VALUES)}]
    marks = cfg.markers.get(lane) or {}
    out = []
    tag_ids = [ctx.crm.tag_uuid(t) for t in marks.get("tags") or []]
    tag_ids = [t for t in tag_ids if t]
    if tag_ids:
        out.append({"any_tags": tag_ids, "must_not": dict(gone)})
    list_ids = [ctx.crm.list_uuid(t) for t in marks.get("lists") or []]
    list_ids = [t for t in list_ids if t]
    if list_ids:
        out.append({"any_lists": list_ids, "must_not": dict(gone)})
    return out


def scan(ctx: Ctx, must: dict, lo: date | None = None, hi: date | None = None, depth: int = 0) -> list[dict]:
    """Every row matching `must`, in slices that each fit under the search's 10,000-row wall."""
    today = ctx.now.date() + timedelta(days=1)
    if lo is None:
        n = ctx.crm.count(must)
        if n <= SLICE_MAX:
            return ctx.crm.rows(must, cap=10000)
        lo, hi = FIRST_DAY, today
    m = dict(must)
    m["created"] = [lo.isoformat(), hi.isoformat()]
    n = ctx.crm.count(m)
    if n == 0:
        return []
    if n <= SLICE_MAX or (hi - lo).days <= 1 or depth > 24:
        if n > 10000:
            ctx.out.problem("one day holds %d records in a lane scan; only the first 10,000 were read" % n)
        return ctx.crm.rows(m, cap=10000)
    mid = lo + (hi - lo) // 2
    return scan(ctx, must, lo, mid, depth + 1) + scan(ctx, must, mid, hi, depth + 1)


def run_lanes(ctx: Ctx) -> None:
    st = ctx.stage
    skip = {str(x) for x in (st.get("skip") or [])}
    owners: dict[str, dict] = {}
    streets: dict[str, str] = {}
    per_lane: Counter = Counter()
    for lane in lanes.ORDER:
        if lane == "bulk" or lane in skip:
            continue
        seen = set()
        for must in lane_musts(ctx, lane):
            try:
                rows = scan(ctx, must)
            except Exception as exc:  # noqa: BLE001  (one lane failing must not hide the others)
                ctx.out.problem("could not scan the %s lane: %s" % (lane, str(exc)[:160]))
                continue
            for r in rows:
                u = r.get("uuid")
                ou = (r.get("owner") or {}).get("uuid") or ""
                if not u or not ou or u in seen:
                    continue
                seen.add(u)
                streets[u] = addr(r)
                # The lanes are read highest first, so the first lane a record is seen in is its own.
                owners.setdefault(ou, {}).setdefault(u, lane)
        per_lane[lane] = len(seen)
    lanes.replace_all(owners)
    both = [(ou, recs) for ou, recs in owners.items() if len(set(recs.values())) > 1]
    pairs: Counter = Counter()
    held = 0
    for ou, recs in both:
        top = lanes.top_lane(list(recs.values()))
        lower = sorted({l for l in recs.values() if l != top}, key=lanes.rank)
        pairs["%s and %s" % (top.replace("_", " "), ", ".join(x.replace("_", " ") for x in lower))] += 1
        held += sum(1 for l in recs.values() if l != top)
    ctx.out.found = sum(per_lane.values())
    ctx.out.acted = len(both)
    for k, n in pairs.most_common(8):
        ctx.out.line("%d owner(s) in %s" % (n, k))
    for ou, recs in both[:4]:
        top = lanes.top_lane(list(recs.values()))
        lower = [u for u, l in recs.items() if l != top]
        ctx.out.line("example: held %s (owner is in the %s lane on another record)" % (
            streets.get(lower[0], lower[0][:8]), top.replace("_", " ")))
    sizes = ", ".join("%s %d" % (l.replace("_", " "), per_lane[l]) for l in lanes.ORDER if per_lane.get(l))
    ctx.out.tally.update({"records in the %s lane" % l.replace("_", " "): n for l, n in per_lane.items() if n})
    if both:
        ctx.out.head = ("%d owner(s) sit in two lanes at once; the gate holds %d lower-lane record(s) so each owner "
                        "hears from one program. Lanes read: %s." % (len(both), held, sizes))
    else:
        ctx.out.head = "No owner sits in two lanes. Lanes read: %s." % (sizes or "none")
