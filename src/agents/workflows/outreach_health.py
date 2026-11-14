"""outreach_health: what the brain can see of outreach health, and plainly what it cannot.

Everything here is already on the brain, so the readout costs no request and no money:
  * THE OUTREACH LIMITS the nightly check found broken (limits.py writes each checked day: 25 dials
    a caller ID line, one call per owner a day, one text a day and five a week, the calling hours).
  * THE TOUCH AUDIT's last findings: a dial or a mail piece the gate would have refused.
  * THE CALLER ID LINES, each line's answer rate and average answered length over the last seven
    days, from the shared copy of the call log. Carriers never say a number is labelled spam; the
    truest free signal is the line's own answer rate falling (the caller-reputation-monitor skill's
    first layer, read here from the account log instead of one dialer's export). A line under the
    floor is named, by its last four digits, to rest.
  * THE REGISTRY SCRUB: how much of a lane the do-not-call registry rule takes out.
  * STAGED BATCHES waiting on a person, and a text batch staged for an earlier day (it expires
    unreleased: a text batch goes out on its day or not at all).

What only the texting side can see is said every time, not left out: whether a text reached a
handset, what a carrier refused and why, and a campaign's opt-outs. DataSift's log never receives a
text sent through Smarter Contact or the texting app, so nothing on the brain knows. The readout
says where to read each one.

On its schedule it speaks only when something new needs a person (the morning digest already
carries the broken limits and the waiting batches, and a standing problem said every morning stops
being read). Asked in Slack it always answers.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, slack, store

log = logging.getLogger(__name__)

WORKFLOW = "outreach_health"
TZ = "America/New_York"
SAID = "wf:outreach_health:said"
NOW = None
POLICY = None

NOT_VISIBLE = [
    "Delivery and carrier refusals for texts sent through the texting app (smrtPhone): its deliverability readout on the "
    "siftstack app, `python src/sms_agent/cli.py deliverability` (add `--full` for each sending number). It reads the "
    "carriers' delivery callbacks: delivered, refused, and the reason given.",
    "Smarter Contact campaigns: each campaign's report in Smarter Contact shows its delivered, undelivered and opted-out "
    "contacts. Those texts never reach DataSift's log.",
    "Spam labels on a caller ID: carriers never report them. The caller-reputation-monitor skill reads each line's answer "
    "rate from the dialer's own call log; the line table here is the same signal from the account log.",
]


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _policy():
    if POLICY is not None:
        return POLICY
    try:
        from ..autopilot import policy
        return policy.load()
    except Exception:  # noqa: BLE001
        return None


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def settings() -> dict:
    """The line floors are the caller-reputation-monitor's published bands: a line answered on under
    30% of its dials, or whose answered calls average under 30 seconds, is worth resting; under 15
    dials in the window the sample is too small and the line is not judged."""
    return {"days": int(_f("AGENTS_OUTREACH_LINE_DAYS", 7)), "min_dials": int(_f("AGENTS_OUTREACH_LINE_MIN_DIALS", 15)),
            "answer_min": _f("AGENTS_OUTREACH_LINE_ANSWER_MIN", 30) / 100.0,
            "talk_min_s": _f("AGENTS_OUTREACH_LINE_TALK_MIN_S", 30)}


def _day(d) -> str:
    d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    return "%s %d" % (d.strftime("%b"), d.day)


# ---------------------------------------------------------------------- what the brain can see
def line_health(since: datetime, until: datetime, st: dict) -> list[dict]:
    """Every caller ID line that dialed in the window, with its answer rate and average answered
    length. A line is the number the owner sees ringing, ours, so it is named by its last four."""
    from ..autopilot import activity
    dials, answered, talk = Counter(), Counter(), Counter()
    for e in activity.events(activity.DIAL, since=since, until=until):
        if e["data"].get("direction") != "inbound" and e["data"].get("line"):
            dials[e["data"]["line"]] += 1
    for e in activity.events("owner.call.answered", since=since, until=until):
        if e["data"].get("direction") != "inbound" and e["data"].get("line"):
            answered[e["data"]["line"]] += 1
            talk[e["data"]["line"]] += int(e["data"].get("duration") or 0)
    out = []
    for line, n in dials.items():
        a = answered[line]
        rate = a / float(n)
        avg = talk[line] / float(a) if a else 0.0
        if n < st["min_dials"]:
            state, why = "too few dials to judge", ""
        elif rate < st["answer_min"]:
            state, why = "rest", "answered on %d%% of %d dials" % (round(100 * rate), n)
        elif a and avg < st["talk_min_s"]:
            state, why = "rest", "answered calls average %d seconds over %d dials" % (round(avg), n)
        else:
            state, why = "fine", ""
        out.append({"line": line[-4:], "dials": n, "answered": a, "answer_rate": round(100 * rate), "avg_talk_s": round(avg),
                    "state": state, "why": why})
    return sorted(out, key=lambda r: (r["state"] != "rest", -r["dials"]))


def limits_section(now: datetime, tz: str) -> dict:
    """The last day the nightly check wrote, and the week behind it."""
    from ..autopilot import limits
    today = now.astimezone(ZoneInfo(tz)).date()
    for back in range(0, 4):
        day = (today - timedelta(days=back)).isoformat()
        raw = store.kv_get("autopilot:limits:day:%s" % day)
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        return {"day": day, "breaches": d.get("breaches") or {}, "totals": d.get("totals") or {},
                "week": limits.week_counts(day)}
    return {"day": "", "breaches": {}, "totals": {}, "week": {}}


def audit_section(pol, now: datetime) -> list[dict]:
    """The touch audit's last run. Its findings count only while they are fresh (36 hours): an audit
    that stopped running must not repeat last week's findings as if they were this morning's."""
    out = []
    for s in (getattr(pol, "stages", None) or {}).values():
        if s.kind != "audit":
            continue
        try:
            last = json.loads(store.kv_get("autopilot:last:%s" % s.id) or "{}")
        except ValueError:
            last = {}
        try:
            at = datetime.fromisoformat(str(last.get("at") or ""))
            fresh = now - at <= timedelta(hours=36)
        except ValueError:
            at, fresh = None, False
        out.append({"stage": s.id, "at": last.get("at") or "", "fresh": fresh,
                    "head": last.get("head") or "", "findings": int(last.get("acted") or 0) if fresh else 0,
                    "problems": list(last.get("problems") or [])[:5] if fresh else []})
    return out


def scrub_section(pol) -> dict:
    from ..autopilot import scrub
    lines = []
    for s in (getattr(pol, "stages", None) or {}).values():
        if s.kind == "scrub":
            ln = scrub.recent_line(s.id)
            if ln:
                lines.append(ln)
    return {"lines": lines, "remembered": scrub.count(), "calls": scrub.settings(pol).calls}


def batches_section(now: datetime, tz: str) -> list[dict]:
    from .. import release
    from ..autopilot import ledger
    today = now.astimezone(ZoneInfo(tz)).date().isoformat()
    out = []
    for b in ledger.batches(status="staged"):
        if b.get("kind") == "command":
            continue                   # a script waiting for release reaches no homeowner
        stale = ""
        if b["kind"] in ("text", "heir_text"):
            full = ledger.batch(b["id"]) or {}
            day = next((it.get("day") for it in full.get("items") or [] if it.get("day")), "")
            if day and day < today:
                stale = day
        out.append({"batch": b["id"], "kind": b["kind"], "stage": b["stage"], "count": b["count"], "usd": b["usd"],
                    "created": str(b["created"] or "")[:10], "what": "%d %s" % (b["count"], release.label(b["kind"])),
                    "stale": stale})
    return out


# ---------------------------------------------------------------------- the run
def findings(r: dict) -> list[str]:
    """What needs a person, one plain line each, most urgent first."""
    out = []
    rest = [x for x in r["lines"] if x["state"] == "rest"]
    if rest:
        out.append("Rest the line%s ending %s: %s." % ("" if len(rest) == 1 else "s", ", ".join(x["line"] for x in rest[:3]),
                                                       rest[0]["why"]))
    lim = r["limits"]
    if lim["breaches"]:
        top = sorted(lim["breaches"].items(), key=lambda kv: -kv[1])[0]
        out.append("%d limit(s) broken on %s, the most: %s (%d)." % (sum(lim["breaches"].values()), _day(lim["day"]), top[0], top[1]))
    for a in r["audit"]:
        if a["findings"]:
            out.append("The touch audit found %d touch(es) that should not have happened%s." % (
                a["findings"], (": " + a["problems"][0].split(": ", 1)[-1]) if a["problems"] else ""))
    stale = [b for b in r["batches"] if b["stale"]]
    for b in stale[:2]:
        out.append("Batch %d (%s) was staged for %s and will expire unreleased." % (b["batch"], b["what"], _day(b["stale"])))
    return out


def run(task: dict) -> dict:
    st = settings()
    pol = _policy()
    tz = getattr(pol, "tz", "") or TZ
    now = _now()
    since = now - timedelta(days=st["days"])
    r = {"workflow": WORKFLOW, "day": now.astimezone(ZoneInfo(tz)).date().isoformat(), "lines": line_health(since, now, st),
         "limits": limits_section(now, tz), "audit": audit_section(pol, now), "scrub": scrub_section(pol),
         "batches": batches_section(now, tz), "not_visible": NOT_VISIBLE, "files": [], "run_dir": "", "quiet": False}
    from ..autopilot import activity
    through = activity.synced_through("calls")
    r["log_through"] = through.isoformat(timespec="seconds") if through else ""
    r["findings"] = findings(r)
    if (task.get("source") or "") == "schedule":
        shape = hashlib.sha256("\n".join(sorted({re.sub(r"\d+", "#", f) for f in r["findings"]})).encode()).hexdigest()[:16]
        if not r["findings"] or store.kv_get(SAID) == shape:
            r["quiet"] = True
        else:
            store.kv_set(SAID, shape)
    if not r["quiet"]:
        p = _write(task, r)
        r["files"], r["run_dir"] = [p.name], str(p.parent.parent)
    return r


def _write(task: dict, r: dict) -> Path:
    d = Path(config.RUNS_DIR) / ("%s-%d" % (WORKFLOW, int(task.get("id") or 0))) / "files"
    d.mkdir(parents=True, exist_ok=True)
    md = ["# Outreach health, %s" % _day(r["day"]), ""]
    md += ["## Needs a person", ""] + (["- %s" % f for f in r["findings"]] or ["Nothing."]) + [""]
    md += ["## Caller ID lines, last %d days" % settings()["days"], "",
           "| Line | Dials | Answered | Answer rate | Average answered call | Reading |", "|---|---|---|---|---|---|"]
    md += ["| %s | %d | %d | %d%% | %d s | %s |" % (x["line"], x["dials"], x["answered"], x["answer_rate"], x["avg_talk_s"],
                                                  x["state"] + (": " + x["why"] if x["why"] else "")) for x in r["lines"]]
    if not r["lines"]:
        md.append("| none | | | | | the log copy holds no outbound dial in these days |")
    if not r["log_through"]:
        md.append("")
        md.append("The call log copy has never been read on this box, so the table is empty for that reason.")
    lim = r["limits"]
    md += ["", "## Outreach limits", ""]
    if lim["day"]:
        md.append("Last checked day %s: %s." % (_day(lim["day"]), "; ".join("%d %s" % (n, k) for k, n in sorted(
            lim["breaches"].items(), key=lambda kv: -kv[1])) or "every limit held"))
        if lim["week"]:
            md.append("Broken in the seven days to then: %s." % "; ".join("%d %s" % (n, k) for k, n in sorted(
                lim["week"].items(), key=lambda kv: -kv[1])))
        t = lim["totals"]
        if t:
            md.append("Checked: %d dials on %d line(s), %d text(s); %d text(s) so far this month." % (
                t.get("dials", 0), t.get("lines", 0), t.get("texts", 0), t.get("texts_this_month", 0)))
    else:
        md.append("The nightly limits check has not written a day yet.")
    md += ["", "## Touch audit", ""]
    for a in r["audit"] or [{"stage": "", "head": "No touch audit stage in the playbook.", "problems": [], "fresh": True}]:
        if a["stage"] and not a.get("fresh"):
            md.append("The touch audit has not run in the last 36 hours%s, so it adds nothing here." % (
                " (last run %s)" % _day(a["at"]) if a.get("at") else ""))
            continue
        md.append(a["head"] or "The touch audit has not run yet.")
        md += ["- %s" % p for p in a["problems"]]
    sc = r["scrub"]
    md += ["", "## Do-not-call registry", ""]
    md += ["- %s" % ln for ln in sc["lines"]] or ["- No scrub has run yet."]
    md.append("- %d flagged number(s) remembered across every lane; they are never texted, and %s." % (
        sc["remembered"], "held from calls until the attorney answers" if sc["calls"] == "hold" else "still called"))
    md += ["", "## Batches waiting on a person", ""]
    md += ["- batch %d: %s from %s, staged %s%s" % (b["batch"], b["what"], b["stage"].replace("_", " "), b["created"],
                                                    (", for %s (will expire)" % b["stale"]) if b["stale"] else "")
           for b in r["batches"]] or ["- None."]
    md += ["", "## What the brain cannot see, and where to read it", ""] + ["- %s" % x for x in r["not_visible"]]
    p = d / ("outreach_health_%s.md" % r["day"])
    p.write_text("\n".join(md) + "\n", encoding="utf-8")
    return p


def render(task: dict, result: dict) -> str:
    if result.get("quiet"):
        return ""
    rest = [x for x in result.get("lines") or [] if x["state"] == "rest"]
    lim = result.get("limits") or {}
    broken = sum((lim.get("breaches") or {}).values())
    audit = sum(a["findings"] for a in result.get("audit") or [])
    waiting = len(result.get("batches") or [])
    parts = ["%d limit(s) broken %s" % (broken, _day(lim["day"])) if lim.get("day") else "no limits check yet",
             "touch audit %s" % ("%d finding(s)" % audit if audit else "clean"),
             "%d caller ID line(s) to rest" % len(rest), "%d batch(es) waiting" % waiting]
    lines = [":satellite_antenna: *Outreach health, %s*: %s." % (_day(result["day"]), ", ".join(parts))]
    f = result.get("findings") or []
    if f:
        lines.append(f[0])
    lines.append("Whether a text was delivered or refused by a carrier is read on the texting side (the texting app's "
                 "deliverability readout, Smarter Contact's campaign reports), not here.")
    for name in result.get("files") or []:
        url = slack.file_url(task.get("id") or 0, name)
        if url:
            lines.append("Detail: %s" % url)
    return "\n".join(lines)
