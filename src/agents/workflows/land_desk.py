"""land_desk: buildable lots matched to the builders who buy them, rendered from a finished analysis.

Atlas's workflow (team/roster.yaml). A market in (knox_blount, union_oh, franklin_oh: the folder a
builder land analysis was built in); out comes the Builder Land Desk workbook (buy boxes, the lot list,
lots by builder, teardowns, development tracts, economics) and a short read of it. Naming a builder in
the request adds a CSV of that builder's own fitting lots.

What the brain can run, measured 2026-11-13: it is one shared CPU with 1 GB of memory and no opensearch
MCP. The land desk's inputs are a property index pull through that MCP (one market is a 523 MB file),
county and state GIS layers, and USGS elevation; the land market's scored set alone is 278 MB. None of
that fits or is reachable here. The finished analysis it all reduces to (analysis.json, about 5 MB for
Knox and Blount) does, so the brain renders from it and the heavy steps stay on the workstation:

    python src/builder_land.py --market <m> --fips <fips> --phase all
    then copy output/builder_land/<m>/analysis.json to <LAND_DESK_DIR>/<m>/analysis.json on the volume

LAND_DESK_DIR defaults to the volume's builder_land folder; a workstation run finds the repo's own
output/builder_land. An analysis bigger than LAND_DESK_MAX_MB (default 60) is refused rather than
loaded, because running the machine out of memory stops every other job with it.

Read-only and free.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import builder_land as bl  # noqa: E402

NAME = "land_desk"
TODAY = None       # the selftest pins the date


def _setting(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def roots() -> list[Path]:
    """Where an analysis may sit: the setting (or the volume's folder), then the repo's own output."""
    first = Path(os.environ.get("LAND_DESK_DIR") or Path(config.DATA_DIR) / "builder_land")
    return [first] + ([bl.OUT] if Path(bl.OUT) != first else [])


def find_market(market: str) -> Path | None:
    if not market or any(x in market for x in ("/", "\\", "..")):
        return None
    for r in roots():
        if (r / market / "analysis.json").is_file():
            return r
    return None


def workstation_steps(market: str) -> str:
    return ("Build it on the workstation: python src/builder_land.py --market %s --fips <county fips> --phase all, "
            "then copy output/builder_land/%s/analysis.json to %s/%s/analysis.json on the brain's volume."
            % (market or "<market>", market or "<market>", roots()[0], market or "<market>"))


def builder_lots(a: dict, wanted: str) -> tuple[str, list[dict]]:
    """(the builder's own name, its fitting lots, best fit first) for a builder named in the request.
    A builder is named when its core name (the legal suffix removed: "ALDER HOMES" for Alder Homes LLC)
    appears in what the person wrote; the longest such name wins, so a short one inside it cannot."""
    text = " %s " % bl.norm(wanted or "")
    hits = [b for b in a.get("boxes") or [] if bl.key(b.get("name") or "")
            and " %s " % bl.key(b.get("name") or "") in text]
    if not hits:
        return "", []
    hit = max(hits, key=lambda b: len(bl.key(b.get("name") or "")))
    rows = []
    for x in a.get("lots") or []:
        for mm in x.get("matches") or []:
            if mm.get("key") == hit.get("key"):
                rows.append({"address": x.get("address"), "city": x.get("city"), "zip": x.get("zip"),
                             "acres": x.get("acres"), "fit_score": mm.get("score"), "meters_to_their_lot": mm.get("dist"),
                             "builders_pay_nearby": x.get("ref"), "offer_to_seller": x.get("offer"),
                             "other_builders_who_fit": max(int(x.get("n_fit") or 1) - 1, 0), "owner": x.get("owner"),
                             "mailing": x.get("mailing"), "datasift_id": x.get("id")})
    rows.sort(key=lambda r: -(r["fit_score"] or 0))
    return bl.pretty(hit.get("name") or ""), rows


def run(task: dict) -> dict:
    payload = store.payload_of(task)
    market = str((payload.get("inputs") or {}).get("market") or "").strip().lower()
    folder = _folder(task)
    files = folder / "files"
    base = {"market": market, "run_dir": str(folder)}
    root = find_market(market)
    if not root:
        return dict(base, ok=False, problem="no land desk analysis for %r on this box. %s" % (market, workstation_steps(market)))
    path = root / market / "analysis.json"
    mb = path.stat().st_size / 1e6
    cap = _setting("LAND_DESK_MAX_MB", 60)
    if mb > cap:
        return dict(base, ok=False, problem="the analysis is %.0f MB, over the %g MB this box loads (LAND_DESK_MAX_MB); "
                                             "render it on the workstation: python src/builder_land.py --market %s "
                                             "--phase export" % (mb, cap, market))
    a = json.loads(path.read_text(encoding="utf-8"))
    keep = bl.OUT
    bl.OUT = root
    try:
        bl.phase_export(SimpleNamespace(market=market, per_builder=75, out_dir=str(files)))
    finally:
        bl.OUT = keep
    made = sorted(p.name for p in files.glob("*_Builder_Land_Desk_*.xlsx"))
    named, lots = builder_lots(a, payload.get("details") or "")
    if lots:
        name = "%s_%s_lots.csv" % (market, "_".join(named.split()).lower()[:40])
        with (files / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(lots[0]))
            w.writeheader()
            w.writerows(lots)
        made.append(name)
    m = a.get("meta") or {}
    analyzed = str(m.get("analyzed") or "")[:10]
    try:
        age = ((TODAY or date.today()) - date.fromisoformat(analyzed)).days if analyzed else None
    except ValueError:
        age = None
    boxes = a.get("boxes") or []
    fit = [x for x in a.get("lots") or [] if x.get("n_fit")]
    return dict(base, ok=True, name=m.get("name") or market, analyzed=analyzed, age_days=age, verdict=bl.verdict(a),
                builders=len(boxes), lots_fit=len(fit), lots_fit_3=sum(1 for x in fit if x["n_fit"] >= 3),
                teardowns=len(a.get("teardowns") or []), tracts=len(a.get("tracts") or []),
                top_builders=[(bl.pretty(b.get("name") or ""), b.get("buys"), b.get("matched_lots"))
                              for b in sorted(boxes, key=lambda b: -(b.get("buys") or 0))[:3]],
                builder=named, builder_lots=len(lots), files=made,
                at=datetime.now().isoformat(timespec="seconds"))


def render(task: dict, result: dict) -> str:
    if not result.get("ok"):
        return "Land desk for %s did not run: %s" % (result.get("market") or "that market", result.get("problem"))
    lines = ["Land desk, %s (analysis built %s%s): %s" % (
        result["name"], result.get("analyzed") or "on an unknown date",
        ", %d days ago" % result["age_days"] if result.get("age_days") is not None else "", result["verdict"])]
    lines.append("%d builders buy here; %d lots owned by individuals fit at least one of them, %d fit three or more; "
                 "%d teardowns and %d development tracts." % (result["builders"], result["lots_fit"],
                                                             result["lots_fit_3"], result["teardowns"], result["tracts"]))
    if result.get("top_builders"):
        lines.append("Most active: " + "; ".join("%s (%s buys, %s lots fit)" % b for b in result["top_builders"]) + ".")
    if result.get("builder"):
        lines.append("%s: %d fitting lots in the CSV." % (result["builder"], result["builder_lots"]))
    if result.get("age_days") is not None and result["age_days"] > 45:
        lines.append("The analysis is over 45 days old; deeds have moved since. Re-run it on the workstation.")
    for n in result.get("files") or []:
        u = slack.file_url(task["id"], n)
        lines.append("%s: %s" % (n, u) if u else n)
    return "\n".join(lines)
