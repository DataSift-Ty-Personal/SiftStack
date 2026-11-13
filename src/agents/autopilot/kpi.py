"""The KPI catalog (build plan 6.1): one file of figures, one loader.

Every report this system writes is judged against a number: a caller's floor, a funnel ratio, the
threshold of an alarm. Those numbers used to live wherever a report happened to need one. The KPI
skill's catalog said 5 to 8 conversations a day while the newest teaching says 20 to 25, the daily
capture carried its own dial floor in the playbook, and nothing recorded which of them the owner
had agreed to. So the numbers are set ONCE, in team/kpi.yaml, and every module reads them here.

Three rules the loader holds:

  * A figure the code needs and the file lacks is a loud problem, never a default. `REQUIRED` lists
    every figure a module reads; a file without one does not load, and the report that wanted it
    says which name is missing. A number hidden in the code is exactly what this file replaces.
  * Every figure says where it came from (a decision, a challenge rule, a lesson and its timestamp,
    a measurement) and whether the owner has confirmed it. A report that leans on a figure nobody
    confirmed says so, in one line, every time.
  * Every problem in the file is reported together, the way the playbook's are, so one read shows
    everything there is to fix.

The second half of the module is what the account log says under the catalog's own definitions. A
CONVERSATION is an answered call of a minute or more, or an inbound text: an answer alone is not
one, because a voicemail greeting answers too (2026-11-12: 185 of 234 dials were answered and 6
were conversations). An OPPORTUNITY is a record entering a lead status from outside one. The goal
sheet, the alarms, the deal-source table, the cadence tuner, the coaching picks and the research
results all count with these functions, so two reports cannot disagree about what a conversation is.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .. import config, store
from . import activity, budget, capture, lanes

log = logging.getLogger(__name__)

STATUSES = ("confirmed", "recommended", "measured", "placeholder")
FIGURE_KEYS = ("value", "low", "high", "unit", "source", "status", "decision", "measured", "says", "note", "name")
INPUT_KEYS = ("monthly_costs", "market_investor_deals_6mo", "marketing_started", "monthly_marketing_budget",
              "runway_months")
PACING_KEYS = ("first_to_market_ready", "ai_lanes", "preset_prefixes", "rules")
RULE_KEYS = ("id", "says", "source", "check")
TOKEN = re.compile(r"\{([a-z0-9_.]+)\}")
UNASSIGNED = "not tied to a caller"

# Every figure a module reads. A catalog without one of these does not load.
REQUIRED = (
    "caller.dials_per_day", "caller.conversations_per_day", "caller.opportunities_per_day", "caller.judge_days",
    "funnel.leads_per_deal", "funnel.dials_per_correct_number", "funnel.dials_per_lead", "funnel.contracts_that_close",
    "alarm.dials_without_lead", "alarm.min_talk_seconds_per_dial", "alarm.min_dials_to_judge", "alarm.dial_jump",
    "alarm.dial_jump_baseline_days", "alarm.answer_rate_min", "alarm.answer_rate_normal", "alarm.first_trace_hit_min",
    "alarm.first_trace_window_days", "alarm.first_trace_min_records", "alarm.flagged_lines_normal",
    "goal.revenue_per_year", "goal.average_deal", "goal.at_risk_share", "goal.activity_window_days",
    "goal.deal_window_days",
    "owner.days_to_first_deal", "owner.net_margin", "owner.market_share_ceiling", "owner.cost_per_deal",
    "source.not_interested_share_of_deals", "channel.leads_per_100_touches",
    "research.check_after_days", "research.reached", "research.contacts_per_deal", "research.min_records_to_judge",
    "cadence.cold_days", "cadence.warm_days", "cadence.hot_days", "cadence.not_interested_auction_days",
    "cadence.not_interested_priority_days", "cadence.not_interested_other_days", "cadence.step_days",
    "cadence.min_sales_to_propose", "cycle.foreclosure_notice_to_sale_days",
    "coaching.best_calls", "coaching.worst_calls", "coaching.new_hire_days",
    "pacing.ai_data_monthly_spend", "pacing.runway_months", "pacing.strategy_hold_days",
    "pacing.no_lead_days_to_change", "pacing.as_is_to_arv", "pacing.repeat_days",
)
REQUIRED_DEFINITIONS = ("conversation", "contact", "opportunity", "correct_number", "deal")


class KpiError(ValueError):
    """The catalog cannot be used. `problems` holds every reason, one plain line each."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super().__init__("team/kpi.yaml has %d problem(s):\n  - %s" % (len(self.problems), "\n  - ".join(self.problems)))


# ---------------------------------------------------------------------- numbers as a person writes them
def num(x) -> str:
    """1,500 and not 1500.0; 13.5 and not 13.50."""
    f = float(x)
    if abs(f - round(f)) < 1e-9:
        return "{:,}".format(int(round(f)))
    return "{:,.2f}".format(f).rstrip("0").rstrip(".")


def money(x) -> str:
    return "$" + num(round(float(x)))


def with_unit(x, unit: str) -> str:
    """A number and its unit. "$ a deal" prints as $15,000 a deal and "% of dials" as 50% of dials."""
    u = (unit or "").strip()
    if u.startswith("$"):
        return ("$%s %s" % (num(x), u[1:].strip())).strip()
    if u.startswith("%"):
        return "%s%s" % (num(x), u)
    return ("%s %s" % (num(x), u)).strip()


@dataclass(frozen=True)
class Figure:
    key: str
    unit: str
    source: tuple
    status: str
    value: float | None = None
    low: float | None = None
    high: float | None = None
    decision: str = ""
    measured: str = ""
    says: str = ""
    note: str = ""
    name: str = ""

    @property
    def ranged(self) -> bool:
        return self.value is None

    @property
    def label(self) -> str:
        return self.name or self.key.split(".")[-1].replace("_", " ")

    def text(self) -> str:
        """The figure in words: "150 dials a day", "20 to 25 conversations a day", "$1,000 to $2,000 a deal"."""
        if not self.ranged:
            return with_unit(self.value, self.unit)
        u = self.unit.strip()
        if u.startswith("$"):
            return ("$%s to $%s %s" % (num(self.low), num(self.high), u[1:].strip())).strip()
        if u.startswith("%"):
            return "%s%% to %s%s" % (num(self.low), num(self.high), u)
        return "%s to %s %s" % (num(self.low), num(self.high), u)

    def tag(self) -> str:
        """How settled the figure is, for a report that quotes it. Empty when the owner confirmed it."""
        if self.status == "recommended":
            return "recommended, not yet confirmed" + (", waits on %s" % self.decision if self.decision else "")
        if self.status == "placeholder":
            return "a placeholder until the owner sets it"
        if self.status == "measured":
            return "measured %s" % self.measured
        return ""


@dataclass
class Catalog:
    figures: dict
    definitions: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    pacing: dict = field(default_factory=dict)
    path: str = ""

    def fig(self, key: str) -> Figure:
        f = self.figures.get(key)
        if f is None:
            raise KpiError(["the catalog has no figure %r" % key])
        return f

    def value(self, key: str) -> float:
        f = self.fig(key)
        if f.ranged:
            raise KpiError(["figure %r is a range (%s): the report must ask for its low or its high end" % (key, f.text())])
        return float(f.value)

    def low(self, key: str) -> float:
        f = self.fig(key)
        return float(f.value if not f.ranged else f.low)

    def high(self, key: str) -> float:
        f = self.fig(key)
        return float(f.value if not f.ranged else f.high)

    def share(self, key: str) -> float:
        """A percent figure as a fraction: 80 becomes 0.8."""
        return self.value(key) / 100.0

    def text(self, key: str) -> str:
        return self.fig(key).text()

    def cite(self, key: str) -> str:
        """The figure with how settled it is: "150 dials a day (recommended, not yet confirmed, waits on D30)"."""
        f = self.fig(key)
        return f.text() + (" (%s)" % f.tag() if f.tag() else "")

    def definition(self, name: str) -> dict:
        d = self.definitions.get(name)
        if d is None:
            raise KpiError(["the catalog does not define %r" % name])
        return d

    def input(self, key: str):
        """One of the owner's own numbers, or None when it was left blank. Never a guess."""
        if key not in INPUT_KEYS:
            raise KpiError(["%r is not an input the catalog knows" % key])
        return self.inputs.get(key)

    def monthly_costs(self) -> dict:
        """name -> dollars a month, only the costs the owner actually typed in."""
        return {str(k): float(v) for k, v in (self.inputs.get("monthly_costs") or {}).items() if v is not None}

    def fill(self, text: str) -> str:
        """Replace each {figure.key} in a sentence with the figure in words."""
        return TOKEN.sub(lambda m: self.text(m.group(1)), text or "")

    def footnote(self, keys) -> str:
        """One line on how settled the figures a report used are. Every report that judges something
        ends with it, so nobody reads a recommended floor as an agreed one."""
        figs = [self.fig(k) for k in dict.fromkeys(keys)]
        rec = [f for f in figs if f.status == "recommended"]
        ph = [f for f in figs if f.status == "placeholder"]
        parts = []
        if rec:
            ds = sorted({f.decision for f in rec if f.decision})
            parts.append("%d of the %d figures used %s recommended and not yet confirmed%s" % (
                len(rec), len(figs), "is" if len(rec) == 1 else "are", " (%s)" % ", ".join(ds) if ds else ""))
        if ph:
            names = " and ".join("the %s" % f.label for f in ph)
            parts.append("%s %s until the owner sets %s" % (names, "is a placeholder" if len(ph) == 1 else "are placeholders",
                                                        "it" if len(ph) == 1 else "them"))
        if not parts:
            return "Judged against team/kpi.yaml; every figure used is confirmed."
        return "Judged against team/kpi.yaml: %s." % "; ".join(parts)


# ---------------------------------------------------------------------- reading and checking the file
def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _strings(v) -> list[str]:
    if v is None:
        return []
    items = v if isinstance(v, (list, tuple)) else [v]
    return [str(x).strip() for x in items if str(x).strip()]


def _is_day(v) -> bool:
    if isinstance(v, date):
        return True
    try:
        date.fromisoformat(str(v))
        return True
    except ValueError:
        return False


def _figure(key: str, f, problems: list[str]) -> Figure | None:
    before = len(problems)
    if not isinstance(f, dict):
        problems.append("figure %s must be a mapping with a value, a unit, a source and a status" % key)
        return None
    extra = sorted(k for k in f if k not in FIGURE_KEYS)
    if extra:
        problems.append("figure %s has %s (a figure takes %s)" % (key, ", ".join(map(str, extra)), ", ".join(FIGURE_KEYS)))
    one = f.get("value") is not None
    rng = f.get("low") is not None or f.get("high") is not None
    if one == rng:
        problems.append("figure %s needs either a value or a low and a high, not %s" % (key, "both" if one else "neither"))
    elif one and not _number(f["value"]):
        problems.append("figure %s: value %r is not a number" % (key, f["value"]))
    elif rng:
        if not (_number(f.get("low")) and _number(f.get("high"))):
            problems.append("figure %s: a range needs a number for low and a number for high" % key)
        elif f["low"] > f["high"]:
            problems.append("figure %s: low %s is above high %s" % (key, f["low"], f["high"]))
    if not str(f.get("unit") or "").strip():
        problems.append("figure %s does not say what it counts (unit)" % key)
    if not _strings(f.get("source")):
        problems.append("figure %s does not say where it comes from (source)" % key)
    status = str(f.get("status") or "").strip().lower()
    if status not in STATUSES:
        problems.append("figure %s has status %r (use %s)" % (key, f.get("status"), ", ".join(STATUSES)))
    if status == "measured" and not _is_day(f.get("measured")):
        problems.append("figure %s is measured but does not say on which day (measured: \"YYYY-MM-DD\")" % key)
    if not str(f.get("says") or "").strip():
        problems.append("figure %s does not say what the number means (says)" % key)
    if len(problems) > before:
        return None
    return Figure(key=key, unit=str(f["unit"]).strip(), source=tuple(_strings(f["source"])), status=status,
                  value=float(f["value"]) if one else None, low=float(f["low"]) if rng else None,
                  high=float(f["high"]) if rng else None, decision=str(f.get("decision") or "").strip(),
                  measured=str(f.get("measured") or "").strip(), says=" ".join(str(f["says"]).split()),
                  note=" ".join(str(f.get("note") or "").split()), name=str(f.get("name") or "").strip())


def _definitions(raw, problems: list[str]) -> dict:
    out = {}
    if not isinstance(raw, dict):
        raw = {}
    for name in REQUIRED_DEFINITIONS:
        if name not in raw:
            problems.append("the catalog does not define %r, and a report counts by it" % name)
    for name, d in raw.items():
        if not isinstance(d, dict):
            problems.append("definition %s must be a mapping" % name)
            continue
        if not str(d.get("says") or "").strip():
            problems.append("definition %s does not say what the word means (says)" % name)
        if not _strings(d.get("source")):
            problems.append("definition %s does not say where it comes from (source)" % name)
        if str(d.get("status") or "").strip().lower() not in STATUSES:
            problems.append("definition %s has status %r (use %s)" % (name, d.get("status"), ", ".join(STATUSES)))
        out[str(name)] = dict(d)
    conv = out.get("conversation") or {}
    if conv:
        if not _number(conv.get("min_seconds")) or conv["min_seconds"] <= 0:
            problems.append("definition conversation needs min_seconds, the length of call that counts as one")
        for flag in ("counts_inbound_text", "counts_inbound_call"):
            if not isinstance(conv.get(flag), bool):
                problems.append("definition conversation needs %s as true or false" % flag)
    cn = out.get("correct_number") or {}
    if cn and not isinstance(cn.get("dialer_sources", []), (list, tuple)):
        problems.append("definition correct_number: dialer_sources must be a list of dialer names")
    opp = out.get("opportunity") or {}
    if opp:
        for key in ("credit_days", "settle_minutes"):
            if not _number(opp.get(key)) or opp[key] < 0:
                problems.append("definition opportunity needs %s, a number: it sets which conversation a new lead is credited to" % key)
    return out


def _inputs(raw, problems: list[str]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        problems.append("inputs must be a mapping")
        return {}
    for k in raw:
        if k not in INPUT_KEYS:
            problems.append("inputs has %r (inputs are %s)" % (k, ", ".join(INPUT_KEYS)))
    out = {k: raw.get(k) for k in INPUT_KEYS}
    costs = out.get("monthly_costs")
    if costs is not None:
        if not isinstance(costs, dict):
            problems.append("inputs.monthly_costs must map a cost's name to dollars a month")
            out["monthly_costs"] = None
        else:
            for name, v in costs.items():
                if v is not None and (not _number(v) or v < 0):
                    problems.append("inputs.monthly_costs.%s must be dollars a month or blank" % name)
    for k in ("market_investor_deals_6mo", "monthly_marketing_budget", "runway_months"):
        if out.get(k) is not None and (not _number(out[k]) or out[k] < 0):
            problems.append("inputs.%s must be a number or blank" % k)
    if out.get("marketing_started") is not None:
        if not _is_day(out["marketing_started"]):
            problems.append("inputs.marketing_started must be a day written \"YYYY-MM-DD\" or blank")
        else:
            out["marketing_started"] = str(out["marketing_started"])
    return out


def _pacing(raw, figures: dict, problems: list[str]) -> dict:
    empty = {"rules": [], "first_to_market_ready": "", "ai_lanes": [], "preset_prefixes": {}}
    if raw is None:
        problems.append("the catalog has no pacing page (pacing: rules)")
        return empty
    if not isinstance(raw, dict):
        problems.append("pacing must be a mapping")
        return empty
    for k in raw:
        if k not in PACING_KEYS:
            problems.append("pacing has %r (it takes %s)" % (k, ", ".join(PACING_KEYS)))
    ai = _strings(raw.get("ai_lanes"))
    for lane in ai:
        if lane not in lanes.ORDER:
            problems.append("pacing.ai_lanes has %r, which is not a lane (%s)" % (lane, ", ".join(lanes.ORDER)))
    prefixes = {}
    if raw.get("preset_prefixes") is not None and not isinstance(raw["preset_prefixes"], dict):
        problems.append("pacing.preset_prefixes must map a lane to the words its preset titles start with")
    else:
        for lane, words in (raw.get("preset_prefixes") or {}).items():
            if lane not in lanes.ORDER:
                problems.append("pacing.preset_prefixes has %r, which is not a lane (%s)" % (lane, ", ".join(lanes.ORDER)))
            prefixes[str(lane)] = _strings(words)
    from . import pacing as pacing_mod      # local: pacing reads this module, so it cannot be imported at the top
    rules, seen = [], set()
    for i, r in enumerate(raw.get("rules") or [], 1):
        if not isinstance(r, dict) or not str(r.get("id") or "").strip():
            problems.append("pacing rule %d needs an id" % i)
            continue
        rid = str(r["id"]).strip()
        if rid in seen:
            problems.append("pacing rule %s is written twice" % rid)
        seen.add(rid)
        for k in r:
            if k not in RULE_KEYS:
                problems.append("pacing rule %s has %r (a rule takes %s)" % (rid, k, ", ".join(RULE_KEYS)))
        says = " ".join(str(r.get("says") or "").split())
        if not says:
            problems.append("pacing rule %s does not say the rule (says)" % rid)
        if not _strings(r.get("source")):
            problems.append("pacing rule %s does not say where it comes from (source)" % rid)
        for key in TOKEN.findall(says):
            if key not in figures:
                problems.append("pacing rule %s quotes figure %r, which the catalog does not have" % (rid, key))
        check = str(r.get("check") or "").strip()
        if check and check not in pacing_mod.CHECKS:
            problems.append("pacing rule %s names check %r (checks are %s)" % (rid, check, ", ".join(pacing_mod.CHECKS)))
        rules.append({"id": rid, "says": says, "source": _strings(r.get("source")), "check": check})
    if not rules:
        problems.append("pacing has no rules")
    return {"rules": rules, "first_to_market_ready": str(raw.get("first_to_market_ready") or "").strip(), "ai_lanes": ai,
            "preset_prefixes": prefixes}


def parse(data, path: str = "") -> Catalog:
    """The catalog from its parsed YAML, or a KpiError carrying every problem in it."""
    if not isinstance(data, dict):
        raise KpiError(["the file must be a mapping with figures, definitions, inputs and pacing"])
    problems: list[str] = []
    raw = data.get("figures")
    if not isinstance(raw, dict) or not raw:
        problems.append("the catalog has no figures")
        raw = {}
    figures = {}
    for key, f in raw.items():
        fig = _figure(str(key), f, problems)
        if fig is not None:
            figures[fig.key] = fig
    for key in REQUIRED:
        if key not in raw:
            problems.append("the catalog has no figure %r, and a report needs it" % key)
    cat = Catalog(figures=figures, definitions=_definitions(data.get("definitions"), problems),
                  inputs=_inputs(data.get("inputs"), problems), pacing=_pacing(data.get("pacing"), figures, problems),
                  path=path)
    if problems:
        raise KpiError(problems)
    return cat


def path() -> Path:
    return config.TEAM_DIR / "kpi.yaml"


_CACHE: dict = {}
_OVERRIDE: list = [None]


def override(cat: Catalog | None) -> None:
    """Stand a catalog in for the file, or pass None to go back to the file. The selftest's way to
    run a stage against small numbers; nothing in the running system calls it."""
    _OVERRIDE[0] = cat


def load(p: Path | None = None) -> Catalog:
    """The catalog, re-read only when the file changes. Raises KpiError when it cannot be used."""
    if p is None and _OVERRIDE[0] is not None:
        return _OVERRIDE[0]
    p = Path(p or path())
    if not p.is_file():
        raise KpiError(["there is no KPI catalog at %s" % p])
    key = (str(p), p.stat().st_mtime)
    if _CACHE.get("key") != key:
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise KpiError(["the file is not valid YAML: %s" % " ".join(str(exc).split())[:200]])
        _CACHE["catalog"] = parse(data, str(p))
        _CACHE["key"] = key
    return _CACHE["catalog"]


def describe(cat: Catalog | None = None) -> list[str]:
    """The whole catalog as plain lines, one figure each with how settled it is and where it comes
    from: what a person reads to see every number the reports are judged against."""
    cat = cat or load()
    out = []
    for key in sorted(cat.figures):
        f = cat.figures[key]
        out.append("%-44s %s  [%s; %s]" % (key, f.text(), f.tag() or "confirmed", ", ".join(f.source)))
    return out


def for_stage(ctx) -> Catalog | None:
    """The catalog for a stage run. A catalog that does not load stops the stage out loud: each
    problem is posted and nothing is judged against a number the file does not hold."""
    try:
        return load()
    except KpiError as exc:
        for p in exc.problems[:6]:
            ctx.out.problem("KPI catalog: %s" % p)
        ctx.out.head = "Not run: the KPI catalog (team/kpi.yaml) has %d problem(s)." % len(exc.problems)
        return None


# ---------------------------------------------------------------------- this track's own tables and files
_READY: set = set()


def ensure(name: str, ddl: str) -> None:
    """Create one of the measurement tables the first time it is used. They hold what the autopilot
    learned (an alarm it raised, a deal's source, a call it picked), never a change to the account,
    so they are written in plan mode too."""
    key = (str(config.DB_PATH), name)
    if key in _READY:
        return
    with store._LOCK, store._db() as c:
        c.executescript(ddl)
    _READY.add(key)


def save_report(name: str, now: datetime, data: dict) -> Path:
    """Keep a report's full detail on the box's own volume; the channel gets the short version."""
    folder = Path(config.DATA_DIR) / "kpi"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / ("%s_%s.json" % (name, now.strftime("%Y-%m-%d")))
    p.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    return p


# ---------------------------------------------------------------------- days
def zone(tz: str) -> ZoneInfo:
    return ZoneInfo(tz or "America/New_York")


def local_day(when: datetime, tz: str) -> str:
    return when.astimezone(zone(tz)).date().isoformat()


def day_start(day, tz: str) -> datetime:
    d = day if isinstance(day, date) else date.fromisoformat(str(day))
    return datetime(d.year, d.month, d.day, tzinfo=zone(tz))


def weekdays(start: date, end: date) -> int:
    """Mondays to Fridays from start up to, not including, end."""
    n, d = 0, start
    while d < end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def fmt_day(day) -> str:
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return "%s %d" % (d.strftime("%b"), d.day)


def sync_log(ctx, groups, floor_days: int = 45) -> bool:
    """Bring the shared log copy up to now for the groups a report reads, inside the stage's own
    request cap. False (and a posted problem) when a group is still catching up, so a report built on
    a short log says so instead of reading as a quiet week."""
    ok = True
    for g in groups:
        try:
            s = activity.sync(ctx.crm, g, now=ctx.now, floor_days=int(ctx.stage.get("floor_days", floor_days)),
                              max_calls=int(ctx.stage.get("max_log_calls", 400)))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (a log that cannot be read must be said, not crash the report)
            ctx.out.problem("could not read the %s log (%s); the numbers here may be short" % (g, str(exc)[:100]))
            ok = False
            continue
        if not s["complete"]:
            ctx.out.problem("the %s log is still catching up (read through %s); the numbers here may be short" % (
                g, s["through"] or "nothing yet"))
            ok = False
    return ok


def log_reach(types) -> datetime | None:
    """The oldest stored event of these types: how far back the log copy can answer."""
    rows = activity.events(types, limit=1)
    return rows[0]["when"] if rows else None


# ---------------------------------------------------------------------- what the log says, by the catalog's definitions
def conversation_events(cat: Catalog, since: datetime | None = None, until: datetime | None = None,
                        owner: str = "") -> list[dict]:
    """Every conversation in the stored log, oldest first: an outbound call that was answered and
    lasted the catalog's minimum, an inbound call of the same length, an inbound text. Each is
    {"when", "owner", "kind": call | call back | text, "seconds", "caller", "number", "line",
    "call", "recording"}. A short answered call is left out: that is the voicemail greeting."""
    d = cat.definition("conversation")
    min_s = int(d["min_seconds"])
    types = ["owner.call.answered", "owner.call.received"]
    if d["counts_inbound_text"]:
        types.append("owner.sms.received")
    out = []
    for e in activity.events(tuple(types), resource=owner, since=since, until=until):
        data, et = e["data"], e["event_type"]
        secs = int(data.get("duration") or 0)
        if et == "owner.sms.received":
            kind, secs = "text", 0
        elif et == "owner.call.received" or data.get("direction") == "inbound":
            if not d["counts_inbound_call"] or secs < min_s:
                continue
            kind = "call back"
        else:
            if secs < min_s:
                continue
            kind = "call"
        out.append({"when": e["when"], "owner": e["resource"], "kind": kind, "seconds": secs,
                    "caller": str(data.get("caller") or "").strip(), "number": data.get("number") or "",
                    "line": data.get("line") or "", "call": data.get("call") or "",
                    "recording": data.get("recording") or "", "id": e["id"]})
    return out


def _blank_row(day: str, caller: str) -> dict:
    return {"day": day, "caller": caller, "dials": 0, "answered": 0, "talk_s": 0, "owners": 0, "numbers": 0,
            "calls_talked": 0, "call_backs": 0, "texts": 0}


def caller_days(cat: Catalog, since_day: str, until_day: str, tz: str) -> list[dict]:
    """One row per caller per captured day, with conversations counted the catalog's way.

    The daily capture stores dials, answers, talk time and the outbound calls of conversation length.
    The catalog also counts an inbound text and an inbound call, so those are added here from the
    log: once per owner per day, and only for an owner who had no outbound conversation that day. A
    text carries no caller's name, so it goes to the one caller who worked that day, or to its own
    row when two did."""
    rows: dict[tuple, dict] = {}
    for r in capture.rows(since_day=since_day):
        if r["day"] > until_day:
            continue
        row = _blank_row(r["day"], r["caller"])
        row.update(dials=r["dials"] or 0, answered=r["answered"] or 0, talk_s=r["talk_s"] or 0,
                   owners=r["owners"] or 0, numbers=r["numbers"] or 0, calls_talked=r["conversations"] or 0)
        rows[(r["day"], r["caller"])] = row
    start, end = day_start(since_day, tz), day_start(until_day, tz) + timedelta(days=1)
    talked: dict[str, set] = defaultdict(set)
    extra = []
    for ev in conversation_events(cat, since=start, until=end):
        day = local_day(ev["when"], tz)
        if ev["kind"] == "call":
            talked[day].add(ev["owner"])
        else:
            extra.append((day, ev))
    seen = set()
    for day, ev in extra:
        if ev["owner"] in talked[day] or (day, ev["owner"]) in seen:
            continue
        seen.add((day, ev["owner"]))
        named = sorted(c for (d, c) in rows if d == day and c not in ("unknown", UNASSIGNED))
        if ev["caller"] and (day, ev["caller"]) in rows:
            who = ev["caller"]
        else:
            who = named[0] if len(named) == 1 else UNASSIGNED
        row = rows.setdefault((day, who), _blank_row(day, who))
        row["call_backs" if ev["kind"] == "call back" else "texts"] += 1
    for row in rows.values():
        row["conversations"] = row["calls_talked"] + row["call_backs"] + row["texts"]
    return sorted(rows.values(), key=lambda r: (r["day"], r["caller"]))


def dials(since: datetime | None = None, until: datetime | None = None) -> int:
    return sum(1 for e in activity.events(activity.DIAL, since=since, until=until)
               if e["data"].get("direction") != "inbound")


def new_leads(since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    """Records that became a lead in the window: a status change INTO a lead status from outside one,
    each record once. A lead moved from New Lead to Hot Lead is the same lead and is not counted
    twice; a record set straight to Warm Lead on the call is a lead even though it never read New."""
    out, seen = [], set()
    for e in activity.events("property.status.updated", since=since, until=until):
        to = str(e["data"].get("to") or "").strip().lower()
        frm = str(e["data"].get("from") or "").strip().lower()
        if to in lanes.LEAD_STATUSES and frm not in lanes.LEAD_STATUSES and e["resource"] and e["resource"] not in seen:
            seen.add(e["resource"])
            out.append({"record": e["resource"], "when": e["when"], "to": to, "from": frm})
    return out


def status_changes(to_statuses, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    """Records whose status changed to one of these in the window, each record once (its first)."""
    want = {str(s).strip().lower() for s in to_statuses or []}
    out, seen = [], set()
    for e in activity.events("property.status.updated", since=since, until=until):
        to = str(e["data"].get("to") or "").strip().lower()
        if to in want and e["resource"] and e["resource"] not in seen:
            seen.add(e["resource"])
            out.append({"record": e["resource"], "when": e["when"], "to": to})
    return out


def correct_numbers(cat: Catalog, since: datetime | None = None, until: datetime | None = None) -> dict:
    """Numbers marked correct in the window, each number once: {"total", "by_call", "other"}. A mark
    the dialer wrote was confirmed on a call; any other mark came from a text reply or a person."""
    dialers = {str(s).lower() for s in cat.definition("correct_number").get("dialer_sources") or []}
    seen: dict[tuple, str] = {}
    for e in activity.events("owner.phone.status.updated", since=since, until=until):
        if str(e["data"].get("to") or "").upper() not in ("CORRECT", "CORRECT_DNC"):
            continue
        seen.setdefault((e["resource"], e["data"].get("number") or ""),
                        "call" if str(e.get("source") or "").lower() in dialers else "other")
    by_call = sum(1 for v in seen.values() if v == "call")
    return {"total": len(seen), "by_call": by_call, "other": len(seen) - by_call}


def mark(actual: float, needed: float, at_risk_share: float, lower_is_better: bool = False) -> str:
    """behind, at risk or on track: at or past the pace is on track, at or past the at-risk share
    of it is at risk, anything less is behind."""
    if needed <= 0:
        return "on track"
    if lower_is_better:
        ratio = needed / actual if actual > 0 else 0.0
    else:
        ratio = actual / needed
    if ratio >= 1.0:
        return "on track"
    if ratio >= at_risk_share:
        return "at risk"
    return "behind"
