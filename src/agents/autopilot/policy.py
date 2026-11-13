"""The autopilot playbook, team/autopilot.yaml, read and validated.

Every problem in the file is collected and reported together (the roster's rule), so one run of
`cli.py autopilot check` shows the author everything to fix. A stage is:

    <id>:
      kind:     what the engine does (KINDS below)
      owner:    the employee who answers for it (a roster id: analysis, research, sales, ops)
      mode:     off | plan | live
      schedule: one or more schedules in the scheduler's grammar
      presets:  the saved DataSift presets that hold its records, by exact title
      ...       kind-specific settings, documented in the playbook itself

Mode `live` acts only when the box also runs AGENTS_MODE=live. Two switches, on purpose: the file
says what the owner wants, the environment says whether this box may write at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .. import config
from . import gate as gate_mod
from . import lanes as lanes_mod

KINDS = ("assign", "native_trace", "retrace", "score", "auction", "mail", "reactivate", "handoff", "digest",
         "attempts", "screen", "lanes", "revoke", "watchdog", "audit", "capture", "leadcheck", "fullattempt")
MODES = ("off", "plan", "live")
SPEND_BY_KIND = {"assign": "free", "native_trace": "free", "retrace": "paid", "score": "paid",
                 "auction": "free", "mail": "outreach", "reactivate": "free", "handoff": "paid", "digest": "free",
                 "attempts": "free", "screen": "free", "lanes": "free", "revoke": "free", "watchdog": "free",
                 "audit": "free", "capture": "free", "leadcheck": "free", "fullattempt": "free"}
# Kinds that read the account without a preset of their own.
NO_PRESETS = ("digest", "mail", "lanes", "revoke", "watchdog", "audit", "capture", "leadcheck", "fullattempt")
ALLOW_WORDS = ("not_interested", "deceased", "heir", "lead", "in_research", "unknown_status", "listed", "mail_only",
               "exhausted")
SUMMARY_BY_KIND = {
    "assign": "New records handed to a caller the moment they land, balanced by open workload.",
    "native_trace": "Records waiting on a skip trace get DataSift's own (free on the unlimited plan).",
    "retrace": "Records with no callable number traced again with the next source nobody has tried.",
    "score": "Every new number scored and tiered (Dial First to Drop) on the phone itself.",
    "auction": "Auction Soon kept true: tagged inside the window, untagged once the date passes.",
    "mail": "The mail lane dropped on schedule, each piece logged back to the record.",
    "reactivate": "Not-interested records whose wait is over handed back to a caller.",
    "handoff": "Records in a preset handed to a teammate's workflow (a deep prospect, a quick screen), a few a day.",
    "digest": "The morning readout: what moved, what it cost, what waits on a person.",
    "attempts": "The call counter written from the account's own call log, so a record moves on whichever dialer made the call.",
    "screen": "New records checked for a death before any text or assignment, and held for research when one may have happened.",
    "lanes": "Each owner placed in one lane across all of their records; an owner sitting in two lanes is listed.",
    "revoke": "An opt-out, a death, a sale or a new lead pulls the owner out of everything already queued.",
    "watchdog": "Yesterday's sequence triggers checked against what each sequence should have produced; only failures are posted.",
    "audit": "Every call, text and mail piece in the day's log checked against the gate; anything that should not have gone out is named.",
    "capture": "Each caller's day written down from the call log: dials, answers, conversations and talk time, whichever dialer was used.",
    "leadcheck": "Every lead checked for an open task, an assignee and a confirmed number; what is missing goes to the lead manager.",
    "fullattempt": "The owners dialed today who still have a top-tier number nobody tried, listed while there is time to finish them.",
}
SOURCES = ("smartskip", "tracerfy", "enformion")
AFTER = ("mail_only", "deep_prospect", "none")
PIECES = ("handwritten_letter", "family_postcard", "soft_offer_check", "postcard", "letter")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class PolicyError(ValueError):
    pass


@dataclass
class Stage:
    id: str
    kind: str
    owner: str
    mode: str
    summary: str
    schedule: list[str]
    presets: list[str]
    params: dict = field(default_factory=dict)

    @property
    def spend(self) -> str:
        return SPEND_BY_KIND[self.kind]

    def get(self, key: str, default=None):
        v = self.params.get(key, default)
        return default if v is None else v


@dataclass
class Policy:
    tz: str
    hours: tuple
    days: frozenset
    daily_usd: float
    record_usd: float
    callers: list[str]
    stages: dict[str, Stage]
    path: str = ""
    outreach_daily_usd: float = 60.0
    gate: object = None          # gate.GateConfig: the tags, lists and fields the suppression gate reads
    lanes: object = None         # lanes.Lanes: how a lane is recognised, and each lane's timeline

    def in_business_hours(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.tz))
        m = local.hour * 60 + local.minute
        return local.weekday() in self.days and self.hours[0] <= m < self.hours[1]

    def stage(self, sid: str) -> Stage:
        return self.stages[sid]


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def mode_of(v) -> str:
    """YAML 1.1 reads a bare `off` as the boolean False (and `on` as True). Read False as off, never
    as "missing, so plan": a stage the owner switched off must stay off. True is refused."""
    if v is False:
        return "off"
    if v is True:
        return "on"
    return str(v or "plan").strip().lower()


def _hours(text: str, problems: list[str]) -> tuple:
    try:
        a, b = str(text).split("-")
        ha, ma = (int(x) for x in a.split(":"))
        hb, mb = (int(x) for x in b.split(":"))
        start, end = ha * 60 + ma, hb * 60 + mb
        if not 0 <= start < end <= 24 * 60:
            raise ValueError
        return start, end
    except ValueError:
        problems.append("business.hours %r must read like 09:00-18:00" % text)
        return 9 * 60, 18 * 60


def _days(v, problems: list[str]) -> frozenset:
    out = set()
    for d in _list(v) or DAY_NAMES[:5]:
        k = str(d).lower()[:3]
        if k not in DAY_NAMES:
            problems.append("business.days has %r (use Mon..Sun)" % d)
            continue
        out.add(DAY_NAMES.index(k))
    return frozenset(out)


def _check_stage(st: Stage, problems: list[str]) -> None:
    from .. import scheduler  # local: the scheduler imports store, which this module must not need

    p = problems
    if st.kind not in KINDS:
        p.append("stage %s has kind %r (kinds: %s)" % (st.id, st.kind, ", ".join(KINDS)))
        return
    if st.mode not in MODES:
        p.append("stage %s has mode %r (use %s)" % (st.id, st.mode, "/".join(MODES)))
    for s in st.schedule:
        try:
            scheduler.parse(s)
        except scheduler.ScheduleError as exc:
            p.append("stage %s: %s" % (st.id, exc))
    if st.kind not in NO_PRESETS and not st.presets and not (st.kind == "auction" and st.get("scope_tags")):
        p.append("stage %s (%s) needs at least one preset title%s" % (
            st.id, st.kind, " or scope_tags" if st.kind == "auction" else ""))
    lane = st.get("lane")
    if lane is not None and str(lane) not in lanes_mod.ORDER:
        p.append("stage %s: lane %r is not a lane (%s)" % (st.id, lane, ", ".join(lanes_mod.ORDER)))
    bad = [a for a in _list(st.get("gate_allow")) if str(a) not in ALLOW_WORDS]
    if bad:
        p.append("stage %s: gate_allow has %s (words are %s)" % (st.id, bad, ", ".join(ALLOW_WORDS)))
    if st.kind == "attempts":
        c = st.get("counters")
        if c is not None and (not isinstance(c, dict) or any(not isinstance(v, int) or v < 0 for v in c.values())):
            p.append("stage %s: counters must map a preset title to the call counter it holds (0, 1, 2 ...)" % st.id)
        if st.get("first_run", "staged") not in ("staged", "auto"):
            p.append("stage %s: first_run must be staged or auto" % st.id)
    if st.kind == "watchdog":
        rules = _list(st.get("expect"))
        if not rules:
            p.append("stage %s: list what each trigger should produce under expect" % st.id)
        for i, r in enumerate(rules, 1):
            if not isinstance(r, dict) or not isinstance(r.get("when"), dict) or not isinstance(r.get("then"), dict):
                p.append("stage %s: expect %d needs a `when` (the trigger) and a `then` (what the record should show)" % (st.id, i))
    if st.kind == "screen":
        if st.get("on_hit", "tag") not in ("tag", "report"):
            p.append("stage %s: on_hit must be tag or report" % st.id)
    if st.kind == "assign":
        if st.get("strategy", "balanced") not in ("balanced", "round_robin"):
            p.append("stage %s: strategy must be balanced or round_robin" % st.id)
        if not isinstance(st.get("routes", {}), dict):
            p.append("stage %s: routes must map a tag title to caller names" % st.id)
    if st.kind == "retrace":
        bad = [s for s in _list(st.get("sources")) if s not in SOURCES]
        if bad:
            p.append("stage %s: unknown sources %s (the engine can call %s)" % (st.id, bad, ", ".join(SOURCES)))
        if not _list(st.get("sources")):
            p.append("stage %s: list the skip trace sources in the order to try them" % st.id)
        if st.get("after_sources", "mail_only") not in AFTER:
            p.append("stage %s: after_sources must be one of %s" % (st.id, ", ".join(AFTER)))
        if st.get("deceased", "deep_prospect") not in ("deep_prospect", "retrace"):
            p.append("stage %s: deceased must be deep_prospect or retrace" % st.id)
    if st.kind == "auction":
        days = st.get("soon_days", 21)
        if not isinstance(days, int) or not 1 <= days <= 120:
            p.append("stage %s: soon_days must be a whole number from 1 to 120" % st.id)
        crit = st.get("critical_days", 14)
        if not isinstance(crit, int) or not 1 <= crit <= (days if isinstance(days, int) else 120):
            p.append("stage %s: critical_days must be a whole number no larger than soon_days" % st.id)
        bad = [f for f in _list(st.get("date_fields", ["auction_date", "foreclosure_date", "tax_auction_date"]))
               if f not in ("auction_date", "foreclosure_date", "tax_auction_date")]
        if bad:
            p.append("stage %s: unknown date fields %s" % (st.id, bad))
    if st.kind == "mail":
        pieces = _list(st.get("pieces"))
        if not pieces:
            p.append("stage %s: list the pieces (one per mail preset, in order)" % st.id)
        for i, pc in enumerate(pieces, 1):
            if not isinstance(pc, dict) or not pc.get("preset") or pc.get("piece") not in PIECES:
                p.append("stage %s: piece %d needs a preset and a piece kind (%s)" % (st.id, i, ", ".join(PIECES)))
        if st.get("send", "staged") not in ("staged", "auto"):
            p.append("stage %s: send must be staged or auto" % st.id)
    if st.kind == "handoff":
        if not str(st.get("workflow") or ""):
            p.append("stage %s: handoff needs the workflow to hand records to (e.g. deep_prospect)" % st.id)
        if not str(st.get("agent") or ""):
            p.append("stage %s: handoff needs the agent who owns that workflow (e.g. research)" % st.id)
        per = st.get("per_day", 3)
        if not isinstance(per, int) or per < 0:
            p.append("stage %s: per_day must be a whole number" % st.id)
    for key in ("daily_usd", "max_records"):
        v = st.get(key)
        if v is not None and (not isinstance(v, (int, float)) or v < 0):
            p.append("stage %s: %s must be a positive number" % (st.id, key))


def parse(data: dict, employees: set[str] | None = None, path: str = "") -> Policy:
    problems: list[str] = []
    if not isinstance(data, dict):
        raise PolicyError("autopilot.yaml must be a mapping")
    biz = data.get("business") or {}
    tz = str(biz.get("tz") or "America/New_York")
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        problems.append("business.tz %r is not a time zone" % tz)
        tz = "America/New_York"
    caps = data.get("caps") or {}
    stages: dict[str, Stage] = {}
    for sid, s in (data.get("stages") or {}).items():
        if not isinstance(s, dict):
            problems.append("stage %s is not a mapping" % sid)
            continue
        kind = str(s.get("kind") or "")
        known = {"kind", "owner", "mode", "summary", "schedule", "presets"}
        st = Stage(id=str(sid), kind=kind, owner=str(s.get("owner") or ""), mode=mode_of(s.get("mode")),
                   summary=" ".join(str(s.get("summary") or SUMMARY_BY_KIND.get(kind, "")).split()),
                   schedule=[str(x) for x in _list(s.get("schedule"))],
                   presets=[str(x) for x in _list(s.get("presets"))],
                   params={k: v for k, v in s.items() if k not in known})
        if employees is not None and st.owner not in employees:
            problems.append("stage %s is owned by %r, who is not on the roster" % (sid, st.owner))
        _check_stage(st, problems)
        stages[st.id] = st
    if not stages:
        problems.append("autopilot.yaml has no stages")
    pol = Policy(tz=tz, hours=_hours(biz.get("hours") or "09:00-18:00", problems),
                 days=_days(biz.get("days"), problems),
                 daily_usd=float(caps.get("daily_usd") or 25), record_usd=float(caps.get("record_usd") or 1.0),
                 callers=[str(c) for c in _list((data.get("people") or {}).get("callers"))],
                 stages=stages, path=path, outreach_daily_usd=float(caps.get("outreach_daily_usd") or 60),
                 gate=gate_mod.parse(data.get("gate"), problems), lanes=lanes_mod.parse(data.get("lanes"), problems))
    if problems:
        raise PolicyError("autopilot.yaml has %d problem(s):\n  - %s" % (len(problems), "\n  - ".join(problems)))
    return pol


def path() -> Path:
    return config.TEAM_DIR / "autopilot.yaml"


_CACHE: dict = {}


def load(p: Path | None = None, employees: set[str] | None = None) -> Policy | None:
    """The playbook, re-read only when the file changes. None when the install has no autopilot."""
    p = Path(p or path())
    if not p.is_file():
        return None
    key = (str(p), p.stat().st_mtime, tuple(sorted(employees or ())))
    if _CACHE.get("key") != key:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        _CACHE["policy"] = parse(data, employees, str(p))
        _CACHE["key"] = key
    return _CACHE["policy"]


def raw_stages(p: Path | None = None) -> dict:
    """The stages as plain mappings, for the roster to list as workflows without importing the
    engine (team.py reads this; a broken playbook must not stop the roster from loading)."""
    p = Path(p or path())
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return dict(data.get("stages") or {})


def as_workflow(sid: str, s: dict) -> dict:
    """One playbook stage in the roster's workflow shape."""
    kind = str(s.get("kind") or "")
    mode = mode_of(s.get("mode"))
    trig = ["schedule:%s" % x for x in _list(s.get("schedule"))] + ["slack"]
    return {"owner": s.get("owner"), "summary": s.get("summary") or SUMMARY_BY_KIND.get(kind, kind),
            "status": "live", "spend": SPEND_BY_KIND.get(kind, "free"), "triggers": trig,
            "manual": "python src/agents/cli.py autopilot run %s   (mode in the playbook: %s)" % (sid, mode),
            "sources": ["agents.autopilot"], "autopilot": True, "covers": _list(s.get("covers"))}
