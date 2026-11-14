"""Tier calibration (build plan 1.14): do the phone tiers predict what the callers actually find?

The whole call lane rests on one claim: a Dial First number is far more likely to be the owner's
than a Dial Fourth or a Drop number, so only the top two tiers are dialed. That claim comes from
the scoring vendor. This job checks it against the team's own results.

Every time a caller or the dialer marks a number (correct, wrong, dead, do-not-call), DataSift logs
the change. The job takes those marks from the shared log, reads each owner once to see which tier
the marked number carries, and counts, per tier, how the numbers turned out:

    tier          marked   correct   wrong   dead   correct share
    Dial First       212        61     118     33            29%
    ...

A healthy scoring model shows the correct share falling from Dial First to Drop. If it does not,
the tiers are not doing their job in this market and the dial rule (decision D12) should be
revisited before more is spent on scoring.

Read-only, and a night job: one read per owner whose numbers were marked.

    python src/agents/cli.py autopilot calibrate [days]
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from . import activity, gate

log = logging.getLogger(__name__)

OUTCOMES = ("CORRECT", "WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC", "NO_ANSWER")
ORDER = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop", "no tier")


def run(crm, days: int = 45, now: datetime | None = None, max_owners: int = 1500) -> dict:
    now = now or datetime.now(timezone.utc)
    sync = activity.sync(crm, "calls", now=now, floor_days=days, max_calls=400)
    since = now - timedelta(days=days)
    last: dict[tuple, str] = {}
    for e in activity.events("owner.phone.status.updated", since=since):
        to = str(e["data"].get("to") or "").upper()
        n = e["data"].get("number") or ""
        if n and to in OUTCOMES:
            last[(e["resource"], n)] = to            # the latest mark on a number is its outcome
    owners = sorted({o for o, _ in last})
    table: dict[str, Counter] = defaultdict(Counter)
    types: dict[str, Counter] = defaultdict(Counter)
    read = unreadable = 0
    for o in owners[:max_owners]:
        try:
            ow = crm.owner(o)
        except Exception:  # noqa: BLE001
            unreadable += 1
            continue
        read += 1
        tiers = {gate.digits(p.get("number")): (gate.phone_tier(p) or "no tier", str(p.get("type") or "UNKNOWN").upper())
                 for p in (ow.get("phones") or [])}
        for (owner, n), outcome in last.items():
            if owner != o:
                continue
            tier, kind = tiers.get(n, ("no tier", "UNKNOWN"))
            table[tier][outcome] += 1
            types[kind][outcome] += 1
    rows = []
    for tier in ORDER:
        c = table.get(tier)
        if not c:
            continue
        settled = c["CORRECT"] + c["CORRECT_DNC"] + c["WRONG"] + c["WRONG_DNC"] + c["DEAD"]
        correct = c["CORRECT"] + c["CORRECT_DNC"]
        rows.append({"tier": tier, "marked": sum(c.values()), "correct": correct, "wrong": c["WRONG"] + c["WRONG_DNC"],
                     "dead": c["DEAD"], "do_not_call": c["DNC"], "no_answer": c["NO_ANSWER"],
                     "correct_share": round(100.0 * correct / settled, 1) if settled else None})
    out = {"at": now.isoformat(timespec="seconds"), "days": days, "log_complete": sync["complete"], "numbers_marked": len(last),
           "owners_read": read, "owners_not_read": unreadable + max(0, len(owners) - max_owners), "by_tier": rows,
           "by_line_type": {k: dict(v) for k, v in types.items()}}
    folder = Path(config.DATA_DIR) / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("tiers_%s.json" % now.strftime("%Y-%m-%d"))).write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def text(out: dict) -> str:
    lines = ["Tier calibration over the last %d days: %d numbers the team or the dialer marked, on %d owners." % (
        out["days"], out["numbers_marked"], out["owners_read"]),
        "%-12s %7s %8s %6s %5s %9s %14s" % ("tier", "marked", "correct", "wrong", "dead", "no answer", "correct share")]
    for r in out["by_tier"]:
        lines.append("%-12s %7d %8d %6d %5d %9d %14s" % (
            r["tier"], r["marked"], r["correct"], r["wrong"], r["dead"], r["no_answer"],
            ("%.0f%%" % r["correct_share"]) if r["correct_share"] is not None else "n/a"))
    if not out["log_complete"]:
        lines.append("The call log was still catching up, so this is a floor.")
    return "\n".join(lines)
