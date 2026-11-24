"""The KPI tracker (Ty, 2026-11-16): one sheet the operations team reads three times a weekday, and
one short Slack update that says what changed.

WHY. The DataSift side runs its own KPI tracker: a Google Sheet that code rewrites in place at 6am,
noon and 6pm, with one short Slack line and a link. Nobody types into it, and that is the point: the
numbers come from the systems, not from a person's end-of-day count. The operations side had the
same numbers (the daily capture, the goal sheet, the contact rate, the lead clock, the deal sources)
in a dozen Slack posts at a dozen times, each with its own window, and no single place to look. This
stage computes ONE snapshot from what those stages already count and hands it to the sheet writer
and the Slack poster (kpisinks), so a number in Slack is always the number on the sheet.

WHAT IT COUNTS WITH. Every act it counts (a call, a text, a phone status, a stage, a mail piece, a
dollar) comes from the KPI sources (agents/kpi_sources: collect), so a tool connected there (a
Kixie webhook, a texting export) counts here with nothing changed, and an act two sources report
counts once. Conversations are the catalog's own (kpi_sources.conversations over the acts); a call
counts only when a source says it was answered. Everything else is a rule that already exists: the
catalog's words (kpi.py), the goal sheet's needs (goals.needs), the weekly report's lead credit
(workflows/kpi_report.credit), the deal sources' channel and source (dealsource.origin, source_of),
the lead clock's first call and last conversation (leadclock), and the daily capture's roster of who
called each day (capture.rows), which decides whose a text reply is. Qualified leads, tasks and the
board's last column are read from the log copy, because no source reports them.

THE AGENCY'S WORK IS ITS OWN. The agency's files are a built-in source, read on their own for the
agency's rows (calls in its file, leads back, its report's figures). Every other number reads every
other source, so a lead our workflow wrote back for the agency still counts once among the new leads,
as it always did, and the agency's dials never land among our callers'.

GAPS ARE SAID, NEVER ZERO. A number nothing can read yet (texts while no texting tool is connected,
offers until something is named an offer, revenue before a fee field is named, doors per deal) is a
row reading "not tracked yet" with the reason in the tab's note. A zero on this sheet means zero. An
issue the sources raise (two sources that disagree, a source gone quiet, a log copy behind) is a
note on every tab that reads that kind and a problem on the snapshot; it never changes a number.

A SOURCE THAT FAILS FAILS ITS OWN TAB. Each tab is built on its own; one that cannot be built is
returned failed with its problem, so the sheet keeps its last good version, while every other tab is
rebuilt. A figure the catalog is missing stops the tab that needs it, with the missing name.

THREE RUNS A WEEKDAY (07:45, 12:30, 19:30 by default): the morning reads the last calling day final
and the month's pace, the noon run the morning's calling block, the evening run the day final and
the day's alarms. Which run it is comes from the stage's own schedule slot (or the `run` setting).
A tab only worth rebuilding at some runs (Lists and Costs in the morning, Direct Mail morning and
evening) is carried from the last snapshot on the others, and its note says when it was built.

READ ONLY, in plan mode and live. It writes nothing to DataSift: its own snapshot file and the
latest snapshot in the kv store are all it keeps. Its live reads are counts through the documented
records search (a preset, a list, a tag, a date range), at most `max_counts` a run, and the lead rows
(their task counts). Everything else is the brain's own log copy and tables. It never posts through
the routed channels itself: kpisinks writes the sheet and posts the one daily message.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid as uuid_mod
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import kpi_sources as ks
from .. import store, voice
from ..kpi_sources import registry as source_registry
from . import (activity, auctiontasks, budget, capture, contactrate, dealsource, ftmpipeline, goals, kpi, lanes,
               leadclock, leadroutes, leadtasks, ledger, mail, windows)
from .stages import Ctx, Summary

log = logging.getLogger(__name__)

GAP = "not tracked yet"
NOT_READ = "not read this run"
NONE_YET = "-"
RUNS = ("morning", "noon", "evening")
TAB_ORDER = ("Scoreboard", "Today", "Callers", "Texts", "Leads", "Deals", "First to Market", "Direct Mail", "Channels",
             "Lists", "Costs", "Goals", "How it is counted")
KV_LATEST = "autopilot:kpitracker:latest"
KV_BUILT = "autopilot:kpitracker:built"
KV_FACTS = "autopilot:kpitracker:facts"
UNKNOWN = "unknown"                      # capture.day_rows' word for dials no caller's name came with
NOT_TIED = kpi.UNASSIGNED                # "not tied to a caller"
# Phone marks other than correct, each family counted once per number (correct_dnc is a correct number
# AND a do-not-call mark, the way Tyler's tracker kept "DNC Correct" and "DNC Wrong" apart). The words
# are the sources' one spelling of a phone status (kpi_sources.phone_status_of).
MARK_FAMILIES = (("Wrong", ("wrong", "wrong_dnc")), ("Dead", ("dead",)), ("DNC", ("dnc", "correct_dnc", "wrong_dnc")))
CORRECT = ("correct", "correct_dnc")
# A lane as a table cell reads it (lanes.NAMES is the word inside a sentence: "first-to-market").
LANE_LABELS = {"lead": "Leads", "heirs": "Heirs", "hot_auction": "Hot auction", "ftm": "First to market",
               "obituary": "Obituary", "top1000": "Top 1,000", "agency": "Agency"}
MAIL_BATCH_KINDS = ("mail", "heir_mail", "obituary_mail")
MAIL_STAGE_KINDS = ("mail", "heirlane", "obituary", "mailcadence")
CADENCE_NAMES = {"sale_date": "Auction", "filed_date": "Probate"}     # a date-timed campaign, named by its clock
CANNOT_ORDER = "cannot be ordered yet"
# The kinds each read asks the sources for. Calls and texts are read together because a conversation is
# derived from both; the agency's files are read on their own, for the agency's rows.
GROUP_KINDS = {"calls": ("call", "text"), "phone": ("phone_status",), "stage": ("stage",), "mail": ("mail",),
               "spend": ("spend",), "agency": ("call", "phone_status", "stage")}
KIND_WORDS = {"call": "calls", "text": "texts", "phone_status": "phone statuses", "stage": "stages", "mail": "mail pieces",
              "spend": "spend"}
EVER = datetime(2020, 1, 1, tzinfo=timezone.utc)                    # "to date": the ledger holds nothing older
CONNECT_TEXTING = ("python src/agents/cli.py kpisources connect <name> <adapter> --tool <tool> (a webhook for a tool "
                   "that posts, or file_import with rows text for an export)")
BOARD_COLUMNS = ftmpipeline.COLUMNS
NO_FIGURE = "no figure in the catalog"
PHONE_SHAPE = re.compile(r"\d{7,}|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}")
CALLER_ID = re.compile(r"caller id", re.I)

# Every catalog figure a tab reads. A tab that needs one the file lacks fails with its name.
USES = ("caller.dials_per_day", "caller.conversations_per_day", "caller.opportunities_per_day", "caller.judge_days",
        "funnel.dials_per_correct_number", "funnel.dials_per_lead", "funnel.leads_per_deal",
        "funnel.contracts_that_close", "funnel.walk_to_contract", "role.lead_manager_attempts_per_day",
        "role.lead_manager_passoffs_per_day", "role.closer_offers_per_day", "measured.talk_seconds_per_dial",
        "alarm.answer_rate_normal", "goal.revenue_per_year", "goal.average_deal", "goal.at_risk_share",
        "goal.activity_window_days", "goal.deal_window_days", "owner.cost_per_deal", "owner.net_margin",
        "source.not_interested_share_of_deals", "source.rehash_share_of_deals", "source.exhausted_callbacks_per_deal",
        "channel.leads_per_100_touches",
        "channel.smarter_contact_leads_per_day", "channel.calling_agency_leads_per_day", "research.reached",
        "research.contacts_per_deal", "cadence.hot_days", "cadence.warm_days", "cadence.cold_days",
        "pacing.runway_months", "cycle.list_turnover_per_month")

DEFAULTS = {
    "run": "auto",                       # auto reads the schedule slot; or morning, noon, evening
    "months": 6,                         # months across the Scoreboard and Costs
    "weeks": 6,                          # weeks (Monday to Sunday) under the Scoreboard
    "caller_days": 20,                   # business days in the Callers daily tracker
    "lead_days": 5,                      # business days in the Leads daily block
    "sync": True,                        # bring the log copy up to now first (kpi.sync_log)
    "groups": ["calls", "state", "texts", "board"],
    "floor_days": 45,
    "max_log_calls": 400,
    "max_counts": 60,                    # live counts through the records search, a run
    "max_lead_rows": 2000,               # lead rows read for their task counts
    "ledger_rows": 200,                  # rows in the lead and contract ledgers
    "qualified_statuses": ["Hot Lead", "Warm Lead", "Cold Lead"],     # F9, the spec's default
    "contract_statuses": ["under_contract"],
    "closed_statuses": ["closed"],
    "lost_statuses": ["lost_deal", "Dead Lead", "not_interested"],
    "board_stage": "",                   # "" finds the playbook's ftmpipeline stage
    "pipeline_stages": ["top1000_attempts"],   # whose presets the Today tab counts (the Top 1,000 call and text presets)
    "deal_stage": "",                    # "" finds the dealsource stage (fee_field, mail_tracking_lines)
    "agency_stage": "",                  # "" finds the agencyscore stage (monthly_fee)
    "research_stage": "",                # "" finds the researchstats stage (researched_tags)
    "auction_stage": "",                 # "" finds the auction stage (critical_days)
    "feeds": ["Foreclosure", "Probate", "Liens", "Condemned", "Obituary"],   # the county lists (spec D.8)
    "mail_stages": [],                   # [] reads every mail, heir and obituary stage that is not off
    "not_callers": [],                   # F15: names left out of the caller tables (the owner's own logins)
    "ledger_streets": False,             # F4: the ledgers show record links only, never a street
    "record_url": "https://app.reisift.io/records/properties/%s",   # /records/<uuid> is the list route (datasift_links)
    "tab_runs": {"Direct Mail": ["morning", "evening"], "Lists": ["morning"], "Costs": ["morning"]},
}


# ---------------------------------------------------------------------- settings
def options(st) -> dict:
    out = json.loads(json.dumps(DEFAULTS))
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None and v != "":
            out[k] = v
    for k, lo, hi in (("months", 1, 12), ("weeks", 1, 26), ("caller_days", 1, 60), ("lead_days", 1, 20)):
        try:
            out[k] = max(lo, min(hi, int(out[k])))
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


def _strs(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def block_problems(st) -> list[str]:
    """A typo in the stage's block is refused when the playbook loads, never read as a default."""
    out = []
    sid = getattr(st, "id", "kpi_tracker")
    run = st.get("run")
    if run is not None and str(run).strip().lower() not in ("auto",) + RUNS:
        out.append("stage %s: run is auto, morning, noon or evening" % sid)
    for key, lo, hi in (("months", 1, 12), ("weeks", 1, 26), ("caller_days", 1, 60), ("lead_days", 1, 20),
                        ("max_counts", 0, 500), ("max_lead_rows", 0, 10000), ("ledger_rows", 1, 2000),
                        ("max_log_calls", 0, 5000), ("floor_days", 1, 200)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            out.append("stage %s: %s must be a whole number from %d to %d" % (sid, key, lo, hi))
    for key in ("qualified_statuses", "contract_statuses", "closed_statuses", "lost_statuses", "feeds", "groups",
                "pipeline_stages"):
        v = st.get(key)
        if v is not None and not _strs(v):
            out.append("stage %s: %s must be a list of names" % (sid, key))
    for key in ("mail_stages", "not_callers"):
        v = st.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            out.append("stage %s: %s must be a list of names (empty for none)" % (sid, key))
    groups = st.get("groups")
    if _strs(groups or []):
        bad = [g for g in groups or [] if g not in activity.GROUPS]
        if bad:
            out.append("stage %s: groups has %s (log groups are %s)" % (sid, bad, ", ".join(activity.GROUPS)))
    tr = st.get("tab_runs")
    if tr is not None:
        if not isinstance(tr, dict):
            out.append("stage %s: tab_runs maps a tab to the runs that rebuild it" % sid)
        else:
            for name, runs in tr.items():
                if name not in TAB_ORDER:
                    out.append("stage %s: tab_runs names %r, which is not a tab (%s)" % (sid, name, ", ".join(TAB_ORDER)))
                elif not isinstance(runs, list) or not runs or any(str(r) not in RUNS for r in runs):
                    out.append("stage %s: tab_runs.%s must list runs from %s" % (sid, name, ", ".join(RUNS)))
    for key in ("sync", "ledger_streets"):
        v = st.get(key)
        if v is not None and not isinstance(v, bool):
            out.append("stage %s: %s is true or false" % (sid, key))
    url = st.get("record_url")
    if url is not None and (not isinstance(url, str) or url.count("%s") != 1):
        out.append("stage %s: record_url must hold %%s once, where the record id goes" % sid)
    return out


# ---------------------------------------------------------------------- which run, which days
def which_run(st, now: datetime, tz: str) -> str:
    """morning, noon or evening. The `run` setting wins; otherwise the stage's own fixed schedule slots
    for today are read: the day's first slot is the morning, its last the evening, any between the
    noon. A run before the day's first slot (a look from the command line at 6am) reads as the morning,
    which is what it would say: the last calling day final. With no fixed slot today (a weekend look,
    an every-N-minutes schedule) the hour decides."""
    v = str((st.get("run") if hasattr(st, "get") else "") or "auto").strip().lower()
    if v in RUNS:
        return v
    from .. import scheduler
    slots = set()
    minute = None
    for text in list(getattr(st, "schedule", None) or []):
        try:
            spec = scheduler.parse(text, tz)
        except scheduler.ScheduleError:
            continue
        local = now.astimezone(ZoneInfo(spec.tz))
        minute = local.hour * 60 + local.minute
        if spec.every_min or local.weekday() not in spec.days or (spec.monthday and local.day != spec.monthday):
            continue
        slots.update(h * 60 + m for h, m in spec.times)
    if minute is None:
        local = now.astimezone(ZoneInfo(tz))
        minute = local.hour * 60 + local.minute
    slots_l = sorted(slots)
    if not slots_l:
        return "morning" if minute < 11 * 60 else "noon" if minute < 17 * 60 else "evening"
    past = [s for s in slots_l if s <= minute]
    if not past:
        return "morning"
    i = slots_l.index(past[-1])
    return "morning" if i == 0 else "evening" if i == len(slots_l) - 1 else "noon"


def last_calling_day(today: date, days) -> date:
    """The latest business day before today: Friday on a Monday."""
    d = today - timedelta(days=1)
    for _ in range(8):
        if d.weekday() in days:
            return d
        d -= timedelta(days=1)
    return today - timedelta(days=1)


def _next_month(first: date) -> date:
    return (first.replace(day=28) + timedelta(days=4)).replace(day=1)


def _iso_day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Win:
    """A window of time the tabs count over: [start, end), and its local days [first, nxt)."""
    label: str
    start: datetime
    end: datetime
    first: date
    nxt: date
    current: bool = False

    @property
    def last_day(self) -> date:
        return self.nxt - timedelta(days=1)


def short_label(w: Win) -> str:
    """The DataSift KPI tracker's column words: "Nov (to date)", "Oct"; a week by its Monday, "Nov 16"."""
    if w.nxt - w.first > timedelta(days=7):
        return w.first.strftime("%b") + (" (to date)" if w.current else "")
    return kpi.fmt_day(w.first) + (" (to date)" if w.current else "")


# ---------------------------------------------------------------------- small words
def _int(x) -> int:
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def _plural(n, one: str, many: str = "") -> str:
    return "%s %s" % (kpi.num(n), one if n == 1 else (many or one + "s"))


def _pct(n, d, digits: int = 1):
    return round(100.0 * n / d, digits) if d else NONE_YET


def _ratio(n, d, digits: int = 1):
    if not d:
        return NONE_YET
    return int(round(float(n) / d)) if digits == 0 else round(float(n) / d, digits)


def _first_name(name: str) -> str:
    t = " ".join(str(name or "").split())
    return t.split(" ")[0] if t else ""


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _day_word(d: date) -> str:
    return d.strftime("%A")


def _nice(d: date) -> str:
    return d.strftime("%a %b %d").replace(" 0", " ")


def _local_day(value, tz: str) -> date | None:
    """A due date the log stored, as a business-local day. Either a plain day or a moment."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        if len(s) > 10:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(ZoneInfo(tz)).date()
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _moment(v) -> datetime | None:
    """A moment a source wrote (an ISO string or a datetime), with its zone; None when it cannot be read."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    s = str(v or "").strip()
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _stamp(t: datetime, zone) -> str:
    """A moment as a reader sees it, in the business zone: Wed Nov 18 11:40 AM."""
    return t.astimezone(zone).strftime("%a %b %d %I:%M %p").replace(" 0", " ")


def _cell(v):
    """A value the sheet can hold: a number stays a number, everything else is text, None is blank."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return v
    return str(v)


def tab(name: str, columns: list, rows: list, note: str = "", failed: bool = False) -> dict:
    """One tab in the snapshot contract kpisinks reads. A row with only its first cell filled is a
    block heading; the row after it, when it names columns, is that block's own header."""
    return {"name": name, "note": " ".join(str(note or "").split()), "columns": [str(c) for c in columns],
            "rows": [[_cell(v) for v in r] for r in rows], "failed": bool(failed)}


def failed_tab(name: str, why: str) -> dict:
    return tab(name, [], [], note="Could not be built this run: %s. The sheet keeps the last good version." % why,
               failed=True)


def footnote(cat, keys) -> str:
    """How settled the figures a tab used are. Only figures the catalog holds: a footnote that named a
    missing one would stop every tab, when only the tab that needs the figure should stop."""
    return cat.footnote([k for k in keys if k in cat.figures])


def bench(cat, key: str, who: str = "") -> str:
    """A catalog figure as a benchmark cell: "150 dials a day per caller (recommended, not yet confirmed,
    waits on D30)". The figure is read here, so a figure the catalog lacks stops the tab that asks."""
    f = cat.fig(key)
    return "%s%s%s" % (f.text(), (" " + who) if who else "", (" (%s)" % f.tag()) if f.tag() else "")


def status_word(s) -> str:
    """A status as a person reads it: new_lead is New lead, Hot Lead stays Hot Lead."""
    t = str(s or "").strip()
    if not t:
        return ""
    t = t.replace("_", " ")
    return t[0].upper() + t[1:]


def _n1(x: float):
    """A planning number: whole when it is ten or more, one decimal under that."""
    return round(x) if x >= 10 else round(x, 1)


def lane_label(lane: str) -> str:
    return LANE_LABELS.get(lane, str(lane or "").replace("_", " ").capitalize() or "Not on the lane map")


# ---------------------------------------------------------------------- counting acts (agents/kpi_sources)
UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.I)
RECORD_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def person_name(ref) -> str:
    """How a person a source names is shown: the first name DataSift logs as it is, and the first part
    of an email a tool sends (pat.caller@x reads Pat), so one caller two sources name two ways is one
    row. A bare user id names nobody a reader would know, so it reads as no name."""
    t = " ".join(str(ref or "").split())
    if not t or UUID_SHAPE.match(t):
        return ""
    if "@" in t:
        local = re.split(r"[._+-]", t.split("@", 1)[0])[0]
        return (local[:1].upper() + local[1:].lower()) if local else ""
    return t


def outside_call(a) -> bool:
    """A call the calling agency made. It is the agency's work, shown on its own rows, and never one of
    our callers' dials: those never held it."""
    return a.kind == "call" and a.tool == "agency"


def our_dials(acts) -> list:
    """Our callers' outbound calls, each once however many sources reported it."""
    return [a for a in acts if ks.METRIC_TEST["dials"](a) and not outside_call(a)]


def caller_rows(acts, conv_s: int) -> dict:
    """caller -> one caller's calls in the window: dials, the owners and numbers dialed, the calls a
    source says were answered (an answer is not a conversation), talk time, the answered calls of
    conversation length, the calls a source says were not answered, and the first and last dial (the
    shift Tyler's tracker had typed). The daily capture's rule (capture.day_rows) over acts: a dial no
    source named a caller for goes to the one caller who worked the window, else stays "unknown"."""
    seen: dict = {}
    for a in our_dials(acts):
        who = person_name(a.person_ref) or UNKNOWN
        r = seen.setdefault(who, {"dials": 0, "answered": 0, "conversations": 0, "talk_s": 0, "noanswer": 0,
                                  "owners": set(), "numbers": set(), "at": []})
        r["dials"] += 1
        r["owners"].add(a.owner)
        r["numbers"].add(a.phone)
        r["at"].append(a.at)
        if a.answered is True:
            r["answered"] += 1
            r["talk_s"] += a.seconds or 0
            if (a.seconds or 0) >= conv_s:
                r["conversations"] += 1
        elif a.answered is False:
            r["noanswer"] += 1
    named = [w for w in seen if w != UNKNOWN]
    if UNKNOWN in seen and len(named) == 1:
        u, n = seen.pop(UNKNOWN), seen[named[0]]
        for k in ("dials", "answered", "conversations", "talk_s", "noanswer"):
            n[k] += u[k]
        n["owners"] |= u["owners"]
        n["numbers"] |= u["numbers"]
        n["at"] += u["at"]
    return {w: {"dials": r["dials"], "answered": r["answered"], "conversations": r["conversations"], "talk_s": r["talk_s"],
                "noanswer": r["noanswer"], "owners": len(r["owners"] - {""}), "numbers": len(r["numbers"] - {""}),
                "first": min(r["at"]), "last": max(r["at"])} for w, r in seen.items()}


def caller_rows_by_day(acts, conv_s: int, zone) -> dict:
    """caller_rows over a window of many days, each day on its own and then added up: a dial no source named
    a caller for goes to the one caller who worked that day (capture.day_rows' rule). Over 90 days with more
    than one caller on the log, the window-wide rule leaves the unnamed dials with nobody, and since a
    dial's own event carries no caller while the answered call does, a caller then reads 99% answered."""
    by_day: dict = defaultdict(list)
    for a in acts:
        by_day[a.at.astimezone(zone).date()].append(a)
    out: dict = {}
    for day_acts in by_day.values():
        for who, r in caller_rows(day_acts, conv_s).items():
            t = out.setdefault(who, {"dials": 0, "answered": 0, "conversations": 0, "talk_s": 0, "noanswer": 0})
            for k in t:
                t[k] += r[k]
    return out


def appointment_cards(since: datetime, until: datetime) -> set:
    """Records whose card moved into an appointment column in the window: the second half of the weekly
    report's appointment (workflows/kpi_report.appointments). The DataSift source reports appointment
    tasks and no card move, so the cards are read from the log copy until it carries them."""
    from ..workflows import kpi_report
    titles = None
    out = set()
    for e in activity.events("card.moved", since=since, until=until):
        col = str(e["data"].get("column") or "")
        if kpi_report._ID.match(col):
            titles = kpi_report._column_titles() if titles is None else titles
            col = titles.get(col.lower(), col)
        if "appointment" in col.lower() and e["resource"]:
            out.add(str(e["resource"]).strip().lower())
    return out


def orderable(design, table: dict) -> bool:
    """A date-timed campaign's piece SiftMail can order: its mail type has a price row in the account (the
    mail cadence's own test, mailcadence.usable). No product at all is the design check's problem."""
    t = design.mail_type
    if t is None:
        return True
    try:
        return int(t) in table
    except (TypeError, ValueError):
        return False


def floor_waits() -> dict:
    """stage -> piece id -> [records, the day the longest wait began]: the records whose piece waits on the
    SiftMail floor (mailcadence.WAIT_FACT), filed under the campaign that claims the record."""
    from . import mailcadence as mc
    out: dict = defaultdict(dict)
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (mc.WAIT_FACT,)).fetchall()
    for r in rows:
        try:
            v = json.loads(r["value"] or "null")
        except ValueError:
            continue
        if not isinstance(v, dict) or not v.get("since") or not v.get("design"):
            continue
        claim = ledger.fact(r["record_uuid"], mc.CLAIM_FACT)
        sid = str(claim.get("stage") or "") if isinstance(claim, dict) else ""
        slot = out[sid].setdefault(str(v["design"]), [0, None])
        slot[0] += 1
        try:
            since = date.fromisoformat(str(v["since"])[:10])
        except ValueError:
            continue
        slot[1] = since if slot[1] is None else min(slot[1], since)
    return dict(out)


def _jsonable(o):
    """What json.dumps cannot write itself: an act or a tally as its own plain form, anything else as text."""
    if hasattr(o, "to_dict"):
        return o.to_dict()
    return str(o)


def speed_waits(since: datetime, until: datetime) -> tuple[list[float], int, int]:
    """(minutes from each new lead to its first outbound call, sorted; leads never called; leads whose
    owner the brain does not know yet), for leads that became New Lead in the window. The same reading
    as the digest's speed line (leadclock.speed_lines), which gives text for one day only: the rule
    itself is leadclock.first_call, and the owner is the one the speed job remembered. Proposed home:
    leadclock.speed_waits, with speed_lines reading it."""
    waits, never, unknown = [], 0, 0
    for e in activity.events("property.status.updated", since=since, until=until):
        if str(e["data"].get("to") or "").strip().lower() != "new_lead":
            continue
        info = ledger.fact(e["resource"], "lead_owner")
        if not isinstance(info, dict) or not info.get("owner"):
            unknown += 1
            continue
        called, on_call = leadclock.first_call(info["owner"], e["when"])
        if on_call:
            continue
        if called:
            waits.append((called - e["when"]).total_seconds() / 60.0)
        else:
            never += 1
    return sorted(waits), never, unknown


def median_minutes(waits: list[float]):
    """The digest's median: the middle wait of the sorted list (the upper one of an even count)."""
    return round(waits[len(waits) // 2]) if waits else NONE_YET


def open_tasks(today: date, tz: str, records: set | None = None) -> dict:
    """Tasks made and not done, from the log copy: due today, overdue, no date. Only tasks the copy
    saw made can be seen open, so a task older than the copy is missing and this is a floor. With
    `records`, only tasks on those records (the leads)."""
    made = {}
    for e in activity.events("task.created"):
        t = e["data"].get("task") or ""
        if t and (records is None or e["resource"] in records):
            made[t] = e
    gone = {e["data"].get("task") for e in activity.events(("task.completed", "task.deleted"))}
    out = {"open": 0, "due_today": 0, "overdue": 0, "undated": 0}
    for t, e in made.items():
        if t in gone:
            continue
        out["open"] += 1
        d = _local_day(e["data"].get("due"), tz)
        if d is None:
            out["undated"] += 1
        elif d < today:
            out["overdue"] += 1
        elif d == today:
            out["due_today"] += 1
    return out


def lane_map() -> dict:
    """owner -> (lane, {record: lane}) from the nightly owner-lane map, read once."""
    out = {}
    with store._LOCK, store._db() as c:
        try:
            rows = c.execute("SELECT owner_uuid, lane, records FROM ap_owner_lane").fetchall()
        except Exception:  # noqa: BLE001  (an install that never ran the lane map)
            return out
    for r in rows:
        try:
            recs = json.loads(r["records"] or "{}") or {}
        except ValueError:
            recs = {}
        # The sources write every id in lower case (kpi_sources.Event), so the map is keyed the same way.
        out[str(r["owner_uuid"] or "").strip().lower()] = (r["lane"], recs if isinstance(recs, dict) else {})
    return out


def fact_records(key: str) -> set:
    """Every record that ever had a fact under this key, whatever it holds now: a card the board took
    off keeps its row (its value goes to null), so this is every record that was ever on the board."""
    with store._LOCK, store._db() as c:
        return {r["record_uuid"] for r in c.execute("SELECT record_uuid FROM ap_facts WHERE key=?", (key,)).fetchall()}


# ---------------------------------------------------------------------- the snapshot
class Snap:
    """One run's snapshot. Holds what the run read so the tabs and the Slack lines share it: a number
    in Slack is the number on its tab because both read the same value here."""

    def __init__(self, ctx: Ctx, cat, o: dict, run: str):
        self.ctx, self.cat, self.o, self.run = ctx, cat, o, run
        self.pol = ctx.policy
        self.tz = ctx.policy.tz
        self.zone = ZoneInfo(self.tz)
        self.now = ctx.now
        self.local = self.now.astimezone(self.zone)
        self.today = self.local.date()
        self.days = getattr(ctx.policy, "days", frozenset(range(5)))
        self.counts_left = int(o["max_counts"])
        self.offline = bool(o.get("_offline"))
        self.short: Counter = Counter()          # why a live count was not read
        self.problems: list[str] = []
        self.notes: list[str] = []
        self.facts: dict = {}
        self._c: dict = {}
        self.conv_s = int(cat.definition("conversation")["min_seconds"]) if cat else 60
        self.not_callers = {str(x).strip().lower() for x in o.get("not_callers") or []}
        self._omap = None
        self._lanes = None
        self._conns = None
        self._roster = None
        self._reading: set = set()               # the kinds the tab being built read, for its issue notes
        self.issues: dict = {}                   # what the sources said is wrong -> the kinds it touches
        self.source_rows: dict = {}              # connection -> what this run's reads said of it
        self.source_notes: list[str] = []

    # ---- windows
    def day(self, d: date, current: bool = False) -> Win:
        start = kpi.day_start(d, self.tz)
        end = min(start + timedelta(days=1), self.now) if current else start + timedelta(days=1)
        return Win(_nice(d), start, end, d, d + timedelta(days=1), current)

    @property
    def day_win(self) -> Win:
        """The day the Today tab reads: the last calling day final in the morning, today so far after."""
        if self.run == "morning":
            return self.day(last_calling_day(self.today, self.days))
        return self.day(self.today, current=True)

    def week(self, monday: date) -> Win:
        start = kpi.day_start(monday, self.tz)
        end = start + timedelta(days=7)
        cur = end > self.now
        label = "Week of %s" % kpi.fmt_day(monday) + (" (so far)" if cur else "")
        return Win(label, start, min(end, self.now) if cur else end, monday, monday + timedelta(days=7), cur)

    def weeks(self) -> list[Win]:
        monday = self.today - timedelta(days=self.today.weekday())
        return [self.week(monday - timedelta(days=7 * i)) for i in range(int(self.o["weeks"]) - 1, -1, -1)]

    def month(self, first: date) -> Win:
        nxt = _next_month(first)
        start, end = kpi.day_start(first, self.tz), kpi.day_start(nxt, self.tz)
        cur = end > self.now
        label = first.strftime("%b %Y") + (" (so far)" if cur else "")
        return Win(label, start, min(end, self.now) if cur else end, first, nxt, cur)

    def months(self) -> list[Win]:
        """From the month the call log copy starts (at most `months` back) to this month, oldest first."""
        this = self.today.replace(day=1)
        firsts = [this]
        for _ in range(int(self.o["months"]) - 1):
            firsts.insert(0, (firsts[0] - timedelta(days=1)).replace(day=1))
        reach = kpi.log_reach(activity.CALLS)
        if reach is None:
            firsts = [this]                     # an empty copy: only this month, never months of zeros
        else:
            start = reach.astimezone(self.zone).date().replace(day=1)
            firsts = [f for f in firsts if f >= start] or [this]
        return [self.month(f) for f in firsts]

    def since_days(self, days: int) -> Win:
        start = self.today_start - timedelta(days=days)
        return Win("Last %d days" % days, start, self.now, start.astimezone(self.zone).date(), self.today + timedelta(days=1), True)

    @property
    def today_start(self) -> datetime:
        return kpi.day_start(self.today, self.tz)

    # ---- the brain's own reads, cached for the run
    def _cached(self, key, fn):
        if key not in self._c:
            self._c[key] = fn()
        return self._c[key]

    def omap(self) -> dict:
        if self._omap is None:
            from ..workflows import kpi_report
            self._omap = kpi_report.owner_map()
        return self._omap

    def lanes_map(self) -> dict:
        if self._lanes is None:
            self._lanes = lane_map()
        return self._lanes

    def owner_lane(self, owner: str) -> str:
        return (self.lanes_map().get(str(owner or "").strip().lower()) or ("",))[0] or ""

    def owner_of(self, record: str) -> str:
        """The owner behind a record, from what the brain already knows: no DataSift read."""
        known = ledger.fact(record, "owner_uuid")
        if known:
            return str(known)
        lo = ledger.fact(record, "lead_owner")
        if isinstance(lo, dict) and lo.get("owner"):
            return str(lo["owner"])
        return self.omap().get(record, "")

    # ---- the KPI sources: every act counted here is read through kpi_sources.collect
    def connections(self) -> list[dict]:
        if self._conns is None:
            try:
                self._conns = ks.connections()
            except Exception as exc:  # noqa: BLE001  (an unreadable source table is said, never a crash)
                self.problem("the KPI sources could not be listed (%s), so their numbers are not read" % str(exc)[:100])
                self._conns = []
        return self._conns

    def kinds_of(self, c: dict) -> set:
        try:
            return set(source_registry.adapter(c["adapter"]).kinds(c))
        except Exception:  # noqa: BLE001
            return set()

    @staticmethod
    def is_agency(c: dict) -> bool:
        return c.get("adapter") == "file_import" and (c.get("settings") or {}).get("preset") == "agency"

    def agency_ids(self) -> list[str]:
        return [c["id"] for c in self.connections() if self.is_agency(c)]

    def our_ids(self) -> list[str]:
        """Every connected source but the agency's files: what our callers', texters' and the pipeline's
        numbers read. A lead our workflow wrote back for the agency stays in DataSift's log, so it counts
        once among the new leads, as it always did."""
        return [c["id"] for c in self.connections() if not self.is_agency(c)]

    def texting(self) -> list[dict]:
        """Connected sources that report texts besides DataSift's own log: a texting tool. A totals report
        gives figures, not texts, so it is not one."""
        return [c for c in self.connections() if c.get("adapter") != "datasift_log" and not self.is_agency(c)
                and (c.get("settings") or {}).get("rows") != "tally" and "text" in self.kinds_of(c)]

    def collected(self, w: Win, group: str) -> dict:
        """kpi_sources.collect for one window and one group of kinds, once a run. Whatever the sources say
        is wrong becomes a problem and a note on every tab that reads that kind (absorb); the numbers are
        collect's own."""
        kinds = GROUP_KINDS[group]
        self._reading.update(kinds)
        key = ("acts", group, w.start, w.end)
        if key not in self._c:
            only = self.agency_ids() if group == "agency" else self.our_ids()
            if w.end <= w.start or not only:
                got = {"events": [], "tallies": [], "health": []}
            else:
                got = ks.collect(w.start, w.end, kinds, now=self.now, tz=self.tz, only=only)
                self.absorb(got.get("health") or [], kinds)
            self._c[key] = got
        return self._c[key]

    def acts(self, w: Win, group: str) -> list:
        return self.collected(w, group)["events"]

    def absorb(self, health: list, kinds) -> None:
        """Keep what the sources said about themselves: each source's last report and state for the How it
        is counted tab, every problem (a disagreement, a silent source, a log copy behind) as an issue,
        every note once."""
        conns = {c["id"]: c for c in self.connections()}
        for h in health:
            if h.get("source") == "merge":
                text = " ".join(str(h.get("text") or "").split())
                if not text:
                    continue
                if h.get("level") == "problem":
                    self.issue(text, {h["kind"]} if h.get("kind") else set(kinds))
                elif text not in self.source_notes:
                    self.source_notes.append(text)
                continue
            cid = str(h.get("source") or "")
            c = conns.get(cid) or {"id": cid, "adapter": h.get("adapter") or "", "tool": h.get("tool") or "", "settings": {}}
            mine = set(kinds) & (self.kinds_of(c) or set(kinds))
            row = self.source_rows.setdefault(cid, {"label": h.get("label") or cid, "read": set(), "last_at": None,
                                                    "through": None, "complete": True, "problems": [], "notes": []})
            if not mine:
                continue
            row["read"] |= mine
            for key, pick in (("last_at", max), ("through", min)):
                t = _moment(h.get(key))
                if t is not None:
                    row[key] = t if row[key] is None else pick(row[key], t)
            row["complete"] = row["complete"] and bool(h.get("complete", True))
            for p in h.get("problems") or []:
                p = " ".join(str(p).split())
                if p not in row["problems"]:
                    row["problems"].append(p)
                self.issue(p, mine)
            for n in h.get("notes") or []:
                n = " ".join(str(n).split())
                if n and n not in self.source_notes:
                    self.source_notes.append(n)

    def issue(self, text: str, kinds) -> None:
        """One thing a source said is wrong: a problem on the snapshot (once) and a note on every tab that
        reads one of these kinds. It never changes a number."""
        text = text if text.endswith(".") else text + "."
        if text not in self.issues:
            self.issues[text] = set()
            self.problem(text)
        self.issues[text] |= set(kinds)

    def issue_note(self) -> str:
        said = [t for t, k in self.issues.items() if k & self._reading]
        return (" What the sources said this run (no number above was changed for it): %s" % " ".join(said)) if said else ""

    def party(self, a) -> str:
        """Whom an act was with: its owner, the owner behind its record, else the record or the number."""
        return a.owner or (str(self.owner_of(a.record) or "").strip().lower() if a.record else "") or a.record or a.phone

    def roster(self, day: str) -> set:
        """The callers the daily capture holds for one day: whose a text reply or a call back is, by
        kpi.caller_days' rule."""
        if self._roster is None:
            self._roster = defaultdict(set)
            for r in capture.rows():
                self._roster[r["day"]].add(r["caller"])
        return self._roster.get(day, set())

    def conv(self, w: Win) -> dict:
        """The window's conversations, the catalog's way (kpi_sources.conversations) over our callers'
        calls and every text. A call counts only when a source says it was answered."""
        return self._cached(("conv", w.start, w.end), lambda: ks.conversations(
            [a for a in self.acts(w, "calls") if not outside_call(a)], self.cat, self.tz))

    def calls(self, w: Win) -> dict:
        """The window's calls: our dials, the answered ones and their talk time, the owners dialed, and the
        conversations (an outbound call of the catalog's length, plus the call backs and texts the catalog
        counts, kpi_sources.conversations)."""
        def read():
            dials = our_dials(self.acts(w, "calls"))
            conv = self.conv(w)
            out_c = sum(1 for x in conv["items"] if x["kind"] == "call")
            answered = [a for a in dials if a.answered is True]
            return {"dials": len(dials), "answered": len(answered),
                    "talk_min": round(sum(a.seconds or 0 for a in answered) / 60.0),
                    "conv_out": out_c, "extras": len(conv["items"]) - out_c, "conversations": len(conv["items"]),
                    "owners": len({a.owner for a in dials} - {""}), "length_unknown": int(conv["length_unknown"])}
        return self._cached(("calls", w.start, w.end), read)

    def extras_by_caller(self, w: Win) -> Counter:
        """caller -> the call backs and text replies the window's conversations count, by kpi.caller_days'
        rule: the caller named on it when the daily capture holds that caller for the day, else the one
        caller the capture holds for the day, else not tied to a caller."""
        out: Counter = Counter()
        for x in self.conv(w)["items"]:
            if x["kind"] == "call":
                continue
            on = self.roster(x["day"])
            named = sorted(c for c in on if c not in (UNKNOWN, NOT_TIED))
            ref = person_name(x["act"].person_ref)
            out[ref if ref and ref in on else (named[0] if len(named) == 1 else NOT_TIED)] += 1
        return out

    def stage_records(self, w: Win, stage: str) -> list[dict]:
        """Records that reached a stage in the window, each once, oldest first: [{"record", "when", "sources"}]."""
        def read():
            out, seen = [], set()
            for a in sorted(self.acts(w, "stage"), key=lambda x: x.at):
                if a.stage == stage and a.record and a.record not in seen:
                    seen.add(a.record)
                    out.append({"record": a.record, "when": a.at, "sources": list(a.sources), "author": a.author})
            return out
        return self._cached(("stage", stage, w.start, w.end), read)

    def leads(self, w: Win) -> list[dict]:
        """Records that became a lead in the window (a lead status entered from outside one), each once."""
        return self.stage_records(w, "lead")

    def credit(self, w: Win) -> tuple[Counter, int]:
        from ..workflows import kpi_report
        return self._cached(("credit", w.start, w.end), lambda: kpi_report.credit(self.cat, self.leads(w), self.omap()))

    def changes(self, which: str, w: Win) -> int:
        """Contracts and closings are the sources' stages; a qualified lead is a status move no source
        reports, so it is read from the log copy (kpi.status_changes)."""
        stage = {"contract_statuses": "contract", "closed_statuses": "closed"}.get(which)
        if stage:
            return len(self.stage_records(w, stage))
        return self._cached((which, w.start, w.end),
                            lambda: len(kpi.status_changes(self.o[which], since=w.start, until=w.end)))

    def appts(self, w: Win) -> dict:
        """Appointments: the records any source reports at the appointment stage (an appointment task in
        DataSift), and the cards moved into an appointment column (appointment_cards)."""
        def read():
            tasks = {x["record"] for x in self.stage_records(w, "appointment")}
            cards = appointment_cards(w.start, w.end)
            return {"total": len(tasks | cards), "tasks": len(tasks), "cards": len(cards)}
        return self._cached(("appts", w.start, w.end), read)

    def offers_named(self) -> bool:
        """Something counts offers: the DataSift source's stage map names an offer status, column or tag."""
        from ..kpi_sources import datasift_log
        for c in self.connections():
            if c.get("adapter") == "datasift_log" and any(datasift_log.stage_map(c.get("settings"))["offer"].values()):
                return True
        return False

    def offers(self, w: Win):
        recs = self.stage_records(w, "offer")
        return len(recs) if (recs or self.offers_named()) else GAP

    def correct(self, w: Win) -> dict:
        """Numbers marked correct in the window, each number once: {"total", "by_call", "other"}. A mark a
        dialer wrote was confirmed on a call; any other came from a text reply or a person
        (kpi.correct_numbers' rule over the sources' phone statuses)."""
        def read():
            dialers = {ks.tool_key(s) for s in self.cat.definition("correct_number").get("dialer_sources") or []}
            seen: dict = {}
            for a in sorted(self.acts(w, "phone"), key=lambda x: x.at):
                if a.outcome in CORRECT:
                    seen.setdefault((self.party(a), a.phone), "call" if a.tool in dialers else "other")
            by_call = sum(1 for v in seen.values() if v == "call")
            return {"total": len(seen), "by_call": by_call, "other": len(seen) - by_call}
        return self._cached(("correct", w.start, w.end), read)

    def marks(self, w: Win) -> dict:
        """Numbers marked wrong, dead or do not call in the window, each number once per family: the
        sibling of the correct numbers for the other marks Tyler's tracker kept. A mark does not say who
        set it, so these are the team's, never a caller's."""
        def read():
            seen = {name: set() for name, _ in MARK_FAMILIES}
            for a in self.acts(w, "phone"):
                for name, statuses in MARK_FAMILIES:
                    if a.outcome in statuses:
                        seen[name].add((self.party(a), a.phone))
            return {name: len(v) for name, v in seen.items()}
        return self._cached(("marks", w.start, w.end), read)

    def reach_set(self, w: Win) -> set:
        """The owners reached in the window by the contact rate's rule (contactrate.reached): a
        conversation, or a number marked correct."""
        def read():
            got = {x["party"] for x in self.conv(w)["items"]}
            got |= {self.party(a) for a in self.acts(w, "phone") if a.outcome in CORRECT}
            return {g for g in got if g}
        return self._cached(("reach", w.start, w.end), read)

    def reached(self, w: Win) -> int:
        return len(self.reach_set(w))

    # ---- Kendrick Eid's notes (2026-11-23): the channels apart, the text funnel, a source for every lead
    def avg_talk(self, w: Win):
        """The mean length of a conversation call in the window, as m:ss ("Avg Talk Time per Conversation"),
        or "-" with no conversation call that carries a length."""
        secs = [int(x["act"].seconds) for x in self.conv(w)["items"]
                if str(x["kind"]).startswith("call") and getattr(x["act"], "seconds", None)]
        if not secs:
            return NONE_YET
        s = int(round(sum(secs) / float(len(secs))))
        return "%d:%02d" % (s // 60, s % 60)

    def text_ledger(self, w: Win) -> dict:
        """The texts the autopilot released in the window, from its own text ledger (limits.py): the texts,
        the owners and numbers they went to, opening texts against follow-ups, and each stage's share. A text
        the other system counted is left out, as dealsource.texts_sent does. None for a field when the
        ledger cannot be read."""
        def read():
            from . import limits
            try:
                rows = [r for r in limits.rows(since_day=w.first.isoformat(), until_day=w.nxt.isoformat(),
                                               statuses=("released",)) if r.get("system") != limits.OTHER]
            except Exception:  # noqa: BLE001  (the ledger belongs to another part of the autopilot)
                log.exception("the text ledger could not be read")
                return {"ok": False, "texts": None, "owners": None, "numbers": None, "opening": None, "followup": None,
                        "by_stage": {}, "owner_set": set()}
            by_stage: dict = defaultdict(lambda: {"texts": 0, "owners": set()})
            for r in rows:
                s = by_stage[str(r.get("stage") or "")]
                s["texts"] += 1
                s["owners"].add(str(r.get("owner_uuid") or ""))
            owners = {str(r.get("owner_uuid") or "") for r in rows} - {""}
            opening = sum(1 for r in rows if str(r.get("series") or "") == "opening")
            return {"ok": True, "texts": len(rows), "owners": len(owners), "owner_set": owners,
                    "numbers": len({str(r.get("number") or "") for r in rows} - {""}),
                    "opening": opening, "followup": len(rows) - opening, "by_stage": dict(by_stage)}
        return self._cached(("text_ledger", w.first, w.nxt), read)

    def outreach_texts(self, w: Win):
        """Texts sent before any reply ("Outreach Texts Sent (Pre-Reply)"): the texts the autopilot released
        into the texting tool (its own ledger), or, once a texting tool reports its sends, every text it sent
        to a number before that number's first reply. The tool sees the autopilot's texts too, so its count
        stands alone: never both added together."""
        led = self.text_ledger(w)
        tool = 0
        if self.texts_covered(w):
            firsts = self.first_reply_moments()
            for a in self.acts(w, "calls"):
                if a.kind == "text" and a.direction == "out" and ks.METRIC_TEST["texts_sent"](a):
                    t = firsts.get((self.party(a), a.phone))
                    if t is None or a.at < t:
                        tool += 1
        if tool:
            return tool
        if not led["ok"]:
            return "could not be read"
        return led["texts"]

    def first_reply_moments(self) -> dict:
        """(owner, number) -> the moment of the first text that number ever sent us, read from every text a
        connected texting tool reported. A thread's first reply counts once, in the window it landed in."""
        def read():
            ever = Win("ever", EVER, self.now, EVER.date(), self.today + timedelta(days=1), True)
            first: dict = {}
            for a in sorted(self.acts(ever, "calls"), key=lambda x: x.at):
                if a.kind == "text" and a.direction == "in":
                    first.setdefault((self.party(a), a.phone), a.at)
            return first
        return self._cached("first_replies", read)

    def first_replies(self, w: Win):
        """Owners' first replies in the window ("Initial Text Replies (First reply of text thread)"), or
        not tracked yet while no texting tool reports replies."""
        if not self.texts_covered(w):
            return GAP
        return sum(1 for t in self.first_reply_moments().values() if w.start <= t < w.end)

    def correct_split(self, w: Win) -> dict:
        """Numbers marked correct in the window, each once, by who confirmed it: call (a dialer wrote the
        mark), text (our own key wrote it outside the brain's own stages: the texting app's mark on a reply),
        person (anyone else in DataSift, or a brain stage). Before 2026-11-15 the log does not tell our key
        from a person (author "user"), so a month before that reads text low; counting every mark no dialer
        wrote read October at 157 texts confirmed, before Smarter Contact was texting at all."""
        def read():
            dialers = {ks.tool_key(s) for s in self.cat.definition("correct_number").get("dialer_sources") or []}
            seen: dict = {}
            for a in sorted(self.acts(w, "phone"), key=lambda x: x.at):
                if a.outcome not in CORRECT:
                    continue
                k = (self.party(a), a.phone)
                if k in seen:
                    continue
                seen[k] = "call" if a.tool in dialers else ("text" if a.author == "self" and not a.via else "person")
            out = Counter(seen.values())
            owners = defaultdict(set)
            for (party, _phone), how in seen.items():
                owners[how].add(party)
            return {"total": len(seen), "call": out["call"], "text": out["text"], "person": out["person"],
                    "owners": {k: v - {""} for k, v in owners.items()}}
        return self._cached(("correct_split", w.start, w.end), read)

    def text_marks(self, w: Win) -> dict:
        """Numbers the texting app marked wrong or do not call on a reply in the window (our own key, outside
        the brain's stages), each number once per family."""
        def read():
            seen = {name: set() for name, _ in MARK_FAMILIES}
            for a in self.acts(w, "phone"):
                if a.author != "self" or a.via:
                    continue
                for name, statuses in MARK_FAMILIES:
                    if a.outcome in statuses:
                        seen[name].add((self.party(a), a.phone))
            return {name: len(v) for name, v in seen.items()}
        return self._cached(("text_marks", w.start, w.end), read)

    def reached_split(self, w: Win) -> dict:
        """Owners reached in the window by channel ("Owners Reached per Channel (Call and SMS separate)"): by
        call, a call conversation or a number a dialer marked correct; by text, a text conversation, a first
        reply, or a number the texting app marked correct on a reply. An owner reached both ways counts in
        both; a number a person marked by hand is in neither."""
        def read():
            call, text = set(), set()
            for x in self.conv(w)["items"]:
                (text if x["kind"] == "text" else call).add(x["party"])
            owners = self.correct_split(w)["owners"]
            call |= owners.get("call", set())
            text |= owners.get("text", set())
            if self.texts_covered(w):
                text |= {p for (p, _n), t in self.first_reply_moments().items() if w.start <= t < w.end}
            return {"call": len(call - {""}), "text": len(text - {""})}
        return self._cached(("reached_split", w.start, w.end), read)

    def texts_per_correct(self, w: Win):
        n = self.outreach_texts(w)
        return _ratio(n, self.correct_split(w)["text"]) if isinstance(n, int) else NONE_YET

    def texts_per_reply(self, w: Win):
        n, r = self.outreach_texts(w), self.first_replies(w)
        if r == GAP:
            return GAP
        return _ratio(n, r) if isinstance(n, int) else NONE_YET

    def replies_per_text_lead(self, w: Win):
        r = self.first_replies(w)
        if r == GAP:
            return GAP
        return _ratio(r, self.lead_sources(w)["texts"])

    def texted_before(self, owner: str, when: datetime, days: int = 30) -> bool:
        """The autopilot released a text to this owner in the `days` before `when`."""
        from . import limits
        if not owner or when is None:
            return False
        local = when.astimezone(self.zone)
        try:
            rows = limits.rows(owner_uuid=owner, since_day=(local.date() - timedelta(days=days)).isoformat(),
                               until_day=(local.date() + timedelta(days=1)).isoformat(), statuses=("released",))
        except Exception:  # noqa: BLE001
            return False
        for r in rows:
            at = _moment(r.get("at"))
            if r.get("system") != limits.OTHER and (at is None or at <= when):
                return True
        return False

    def text_confirmed(self) -> dict:
        """owner -> the moments one of their numbers was marked correct outside a call (no dialer wrote it, no
        brain stage did): the texting app's mark on a reply, or a person's in DataSift after a text exchange.
        Measured 2026-11-23: 28 of the 35 November leads no call of ours came before were marked this way first,
        the Smarter Contact reply flow, and the autopilot's own text ledger only starts 2026-11-18."""
        def read():
            dialers = {ks.tool_key(s) for s in self.cat.definition("correct_number").get("dialer_sources") or []}
            ever = Win("ever", EVER, self.now, EVER.date(), self.today + timedelta(days=1), True)
            out: dict = defaultdict(list)
            for a in self.acts(ever, "phone"):
                if a.outcome in CORRECT and a.tool not in dialers and not a.via:
                    out[str(self.party(a) or "").lower()].append(a.at)
            return out
        return self._cached("text_confirmed", read)

    def text_confirmed_before(self, owner: str, when: datetime, days: int = 30) -> bool:
        if not owner or when is None:
            return False
        lo, hi = when - timedelta(days=days), when + timedelta(minutes=10)
        return any(lo <= t <= hi for t in self.text_confirmed().get(owner.lower(), []))

    def lead_source(self, lead: dict) -> tuple[str, str]:
        """(source, owner) for one new lead: caller, call-in, texts, autopilot, or none (lead_sources)."""
        owner = str(ledger.fact(lead["record"], "owner_uuid") or "") or self.omap().get(lead["record"], "")
        o = dealsource.origin(self.cat, owner, lead["when"]) if owner else {"channel": "", "caller": ""}
        channel = str(o.get("channel") or "")
        if o.get("caller"):
            return "caller", owner
        if channel.startswith("call") or channel == "mail":
            return "call-in", owner
        if channel == "text" or self.texted_before(owner, lead["when"]) or self.text_confirmed_before(owner, lead["when"]):
            return "texts", owner
        if lead.get("author") == "self":
            return "autopilot", owner
        if lead.get("author") == "system":
            return "automation", owner                 # DataSift's own user: a dialer's disposition or a sequence
        return "none", owner

    def lead_source_rows(self, w: Win) -> list[tuple[str, str]]:
        return self._cached(("lead_source_rows", w.start, w.end),
                            lambda: [self.lead_source(lead) for lead in self.leads(w)])

    def lead_sources(self, w: Win) -> Counter:
        """Every new lead in the window credited to something (Kendrick Eid: "Leads credited to SOMETHING
        (Autopilot, autosms, AI caller, whatever it is)"): a caller when the log holds a conversation of ours
        before it (the opportunity rule, dealsource.origin); a call-in, or a text reply, when that was the
        conversation; texts when the autopilot texted the owner in the 30 days before; the autopilot when
        our own key set the status; none when nothing points anywhere."""
        out = Counter({"caller": 0, "call-in": 0, "texts": 0, "autopilot": 0, "automation": 0, "none": 0})
        out.update(src for src, _owner in self.lead_source_rows(w))
        return out

    def texts_by_lane(self, w: Win) -> list[list]:
        """The window's autopilot texts by lane (the stage's lane: first to market, the Top 1,000), with the
        owners texted, the numbers a text reply confirmed among them, and the leads from texts among them."""
        led = self.text_ledger(w)
        lanes_of: dict = defaultdict(lambda: {"texts": 0, "owners": set()})
        for stage, v in (led.get("by_stage") or {}).items():
            st = (getattr(self.pol, "stages", None) or {}).get(stage)
            lane = str(st.get("lane") or "") if st is not None else ""
            name = lane_label(lane) if lane else (stage.replace("_", " ") or "other")
            lanes_of[name]["texts"] += v["texts"]
            lanes_of[name]["owners"] |= v["owners"]
        confirmed = self.correct_split(w)["owners"].get("text", set())
        lead_owners = {o for src, o in self.lead_source_rows(w) if src == "texts"}
        out = []
        for name, v in sorted(lanes_of.items(), key=lambda kv: -kv[1]["texts"]):
            owners = v["owners"] - {""}
            out.append([name, v["texts"], len(owners), len(owners & confirmed), len(owners & lead_owners)])
        return out

    def caller_day_rates(self, w: Win) -> dict:
        """This window's dials, conversations and credited leads per caller per calling day: the Goals tab's
        "This month a day" beside each target. A calling day is a caller and a local day with a dial of ours."""
        def read():
            pairs, by = set(), Counter()
            for a in our_dials(self.acts(w, "calls")):
                name = person_name(a.person_ref) or UNKNOWN
                if name in (UNKNOWN, NOT_TIED) or name.strip().lower() in self.not_callers:
                    continue
                pairs.add((name, a.at.astimezone(self.zone).date()))
                by["dials"] += 1
            for x in self.conv(w)["items"]:
                if str(x["kind"]).startswith("call"):
                    by["conversations"] += 1
            n = len(pairs)
            leads = self.lead_sources(w)["caller"]
            return {"days": n, "dials": _ratio(by["dials"], n), "conversations": _ratio(by["conversations"], n),
                    "leads": _ratio(leads, n)}
        return self._cached(("caller_day_rates", w.start, w.end), read)

    def goal_cells(self) -> dict:
        """key -> [needed, so far, pace, mark] for the Goals tab's gray columns (Kendrick Eid and Ty: "Combine
        inputs and goals", "add pace to goals"): each goal worked back to a month beside this month's count,
        its pace to the month's end and the mark; each caller target beside this month's own a calling day."""
        cat = self.cat
        need = goals.needs(cat)
        risk = cat.share("goal.at_risk_share")
        cur = self.months()[-1]
        calls_span = self.month_days(self._reach_day(activity.CALLS))
        state_span = self.month_days(self._reach_day("property.status.updated"))

        def pace(got, span, money=False):
            done, total = span
            if not isinstance(got, (int, float)) or isinstance(got, bool):
                return ""
            if done <= 0:
                return NONE_YET
            return round(got * total / done, 2 if money else 1)

        out = {}
        revenue_month = round(float(cat.value("goal.revenue_per_year")) / 12.0, 2)
        for key, n, got, span, money in (
                ("goal.revenue_per_year", revenue_month, self.revenue(cur), state_span, True),
                ("goal.average_deal", need["deals"], self.changes("closed_statuses", cur), state_span, False),
                ("funnel.contracts_that_close", need["contracts"], self.changes("contract_statuses", cur), state_span, False),
                ("funnel.leads_per_deal", need["leads"], len(self.leads(cur)), state_span, False),
                ("funnel.dials_per_lead", need["dials"], self.calls(cur)["dials"], calls_span, False)):
            p = pace(got, span, money)
            out[key] = [n if money else _n1(n), got, p, kpi.mark(p, n, risk) if isinstance(p, (int, float)) else ""]
        rates = self.caller_day_rates(cur)
        for key, got, lower in (("caller.dials_per_day", rates["dials"], False),
                                ("caller.conversations_per_day", rates["conversations"], False),
                                ("caller.opportunities_per_day", rates["leads"], False),
                                ("funnel.dials_per_correct_number",
                                 _ratio(self.calls(cur)["dials"], self.correct(cur)["total"]), True)):
            f = cat.fig(key)
            target = f.value if f.value is not None else cat.low(key)
            shown = ("%s to %s" % (kpi.num(cat.low(key)), kpi.num(cat.high(key)))) if f.value is None else target
            mark = (kpi.mark(got, cat.high(key) if lower and f.value is None else target, risk, lower_is_better=lower)
                    if isinstance(got, (int, float)) and target else "")
            out[key] = [shown, got, "", mark]
        return out

    def goal_cells_safe(self) -> dict:
        """goal_cells, or {} when a number it needs cannot be read: the Goals tab then keeps its last."""
        if self.cat is None:
            return {}
        try:
            return self.goal_cells()
        except Exception as exc:  # noqa: BLE001  (the sheet's gray columns must never cost the run)
            self.problem("the Goals tab's pace columns could not be worked out (%s)" % str(exc)[:100])
            return {}

    def worked_set(self, w: Win) -> set:
        """The owners worked in the window: an outbound call of ours or a text sent (contactrate.first_worked)."""
        return self._cached(("worked", w.start, w.end), lambda: {
            self.party(a) for a in self.acts(w, "calls") if a.direction == "out" and not outside_call(a)} - {""})

    def texts_covered(self, w: Win) -> bool:
        """A texting tool counts this window: one reported a text in it, or one was connected before it
        ended. A window that ended before any texting tool was connected reads not tracked yet."""
        tools = self.texting()
        if not tools:
            return False
        ids = {c["id"] for c in tools}
        if any(a.kind == "text" and set(a.sources) & ids for a in self.acts(w, "calls")):
            return True
        return any((_moment(c.get("created")) or w.start) < w.end for c in tools)

    def texts_sent(self, w: Win, tool: str = ""):
        """Texts sent in the window from every source (a failed text is not sent), or "not tracked yet"."""
        if not self.texts_covered(w):
            return GAP
        return sum(1 for a in self.acts(w, "calls") if ks.METRIC_TEST["texts_sent"](a) and (not tool or a.tool == tool))

    def replies(self, w: Win, tool: str = ""):
        if not self.texts_covered(w):
            return GAP
        return sum(1 for a in self.acts(w, "calls") if ks.METRIC_TEST["replies"](a) and (not tool or a.tool == tool))

    def texts_note(self) -> str:
        tools = self.texting()
        if not tools:
            return ("Texts sent and their replies are not tracked yet: no texting tool is connected. Connect one with %s."
                    % CONNECT_TEXTING)
        return ("Texts come from %s and the DataSift log, each text counted once; a window that ended before the texting "
                "tool was connected reads not tracked yet." % ", ".join(ks.label(c) for c in tools))

    def offers_note(self) -> str:
        if self.offers_named():
            return "Offers are what the DataSift source's stage map names an offer."
        return ("Offers are not tracked yet: nothing is named an offer (the offer card shares the Appointment Booked "
                "column until plan 2.10). Name one in the DataSift source's stages setting: python src/agents/cli.py "
                "kpisources connect datasift datasift_log --settings with stages.offer.")

    def speed(self, w: Win) -> tuple[list[float], int, int]:
        return self._cached(("speed", w.start, w.end), lambda: speed_waits(w.start, w.end))

    def spend(self, w: Win) -> dict:
        """What the autopilot paid in the window, by channel, from the spend the sources report: mail and the
        data work (skip traces, phone scoring) are the ledger's own rows, research is the deep prospect's
        spend (dealsource.spend's split, on the window's own local days)."""
        def read():
            out = {"mail": 0.0, "data": 0.0, "research": 0.0}
            for a in self.acts(w, "spend"):
                ref = str(a.raw_ref or "")
                if ref.startswith("ap_events:"):
                    out["mail" if a.outcome == "mail" else "data"] += float(a.amount or 0)
                elif ref.startswith("spend:") and a.campaign in store.RESEARCH_WORKFLOWS:
                    out["research"] += float(a.amount or 0)
            return {k: round(v, 2) for k, v in out.items()}
        return self._cached(("spend", w.start, w.end), read)

    def texts_released(self, w: Win):
        return self._cached(("texts", w.first), lambda: dealsource.texts_sent(w.first, w.nxt))

    def check_stage_map(self) -> None:
        """This stage's contract and closed statuses against what the DataSift source counts as one. The
        source's map decides the counts, so a difference is said, never mended silently."""
        from ..kpi_sources import datasift_log
        conn = next((c for c in self.connections() if c.get("adapter") == "datasift_log"), None)
        if conn is None:
            self.problem("the DataSift log is switched off as a KPI source, so calls, texts, phone statuses and stages "
                         "count only what other sources report")
            return
        sm = datasift_log.stage_map(conn.get("settings"))
        for key, stage in (("contract_statuses", "contract"), ("closed_statuses", "closed")):
            mine = sorted({ks.words(s) for s in self.o[key] if ks.words(s)})
            theirs = sorted(set(sm[stage]["statuses"]))
            if mine != theirs:
                self.problem("this stage's %s (%s) differ from what the DataSift source counts as %s (%s), so the source's "
                             "are counted; set stages.%s.statuses on the datasift connection to match (python "
                             "src/agents/cli.py kpisources connect datasift datasift_log --settings ...)" % (
                                 key, ", ".join(mine) or "none", "a " + stage if stage == "contract" else "closed",
                                 ", ".join(theirs) or "none", stage))

    # ---- the playbook's own settings for the stages whose numbers the tracker reads
    def stage_of(self, kind: str, prefer: str = ""):
        stages = getattr(self.pol, "stages", None) or {}
        if prefer and prefer in stages:
            return stages[prefer]
        for st in stages.values():
            if st.kind == kind:
                return st
        return None

    def fee_field(self) -> str:
        st = self.stage_of("dealsource", self.o["deal_stage"])
        return str(st.get("fee_field") or "") if st is not None else ""

    def typed_deals(self) -> list[dict]:
        """The deals typed on the KPI sheet's Goals tab (kpi.apply_typed), each {"property", "signed",
        "closed" (YYYY-MM-DD), "type", "price", "expected", "fee"}. Empty when none was typed."""
        info = getattr(self.cat, "typed", None) or {}
        return [d for d in info.get("deals") or [] if isinstance(d, dict)]

    def revenue(self, w: Win):
        """What deals paid in the window. The deal-source stage's fee field when it is named; else the fees
        typed on the Goals tab, by the day each deal closed (prod-account has no fee field, decision F10)."""
        if self.fee_field():
            rows = dealsource.deals(since_day=w.first.isoformat(), until_day=w.nxt.isoformat())
            return round(sum(float((d.get("detail") or {}).get("fee") or 0) for d in rows))
        deals = self.typed_deals()
        if not deals:
            return GAP
        total = 0.0
        for d in deals:
            day = _iso_day(d.get("closed"))
            if day is not None and d.get("fee") is not None and w.first <= day < w.nxt:
                total += float(d["fee"])
        return round(total)

    def fees_open(self):
        """The expected fees of the deals typed as signed and not yet closed."""
        deals = self.typed_deals()
        if not deals:
            return GAP
        return round(sum(float(d.get("expected") or 0) for d in deals if d.get("signed") and not d.get("closed")))

    def revenue_note(self) -> str:
        if self.fee_field():
            return "Revenue is what the deal-source stage's fee field (%s) recorded at closing." % self.fee_field()
        if self.typed_deals():
            return ("Revenue is the fees typed on the Goals tab's deals, counted on the day each closed (%d deal%s "
                    "typed)." % (len(self.typed_deals()), "" if len(self.typed_deals()) == 1 else "s"))
        return ("Revenue is not tracked until a deal's fee is typed on the Goals tab of the Google Sheet (one row a "
                "contract), or the deal-source stage names a fee field.")

    # ---- live counts, bounded
    def _take(self, what: str) -> bool:
        if self.offline:
            self.short["the preview was asked to read nothing from DataSift"] += 1
            return False
        if self.counts_left <= 0:
            self.short["the run's cap of %d live counts" % int(self.o["max_counts"])] += 1
            return False
        self.counts_left -= 1
        return True

    def count(self, must: dict, what: str = ""):
        """One count through the documented records search, or None when it was not read (the cap, the
        hour's request budget). A clean record is what the app's first tab shows; it is set unless the
        filter names a record type of its own."""
        if not self._take(what):
            return None
        m = dict(must)
        m.setdefault("property_type", "clean")
        try:
            return int(self.ctx.crm.count(m) or 0)
        except budget.BudgetExhausted:
            self.counts_left = 0
            self.short["the hour's request budget"] += 1
            return None

    def preset_count(self, title: str):
        """A saved preset's count, its relative windows resolved the way the digest counts a preset. The
        preset index is one read a run (taken once); each count is one more. The preset sets its own
        record type, so none is added."""
        if "preset_index" not in self._c:
            if not self._take("the preset index"):
                return None
            self._c["preset_index"] = True
        try:
            must = self.ctx.crm.preset_must(title)
        except KeyError:
            return "no preset with this title"
        except budget.BudgetExhausted:
            self.counts_left = 0
            self.short["the hour's request budget"] += 1
            return None
        must, notes = windows.resolve(must, self.now)
        if any("dropped" in n for n in notes):
            self.notes.append("The preset %s carries a window this reader could not convert, so its count is wider "
                              "than the app shows." % title)
        if not self._take(title):
            return None
        try:
            return int(self.ctx.crm.count(must) or 0)
        except budget.BudgetExhausted:
            self.counts_left = 0
            self.short["the hour's request budget"] += 1
            return None

    def list_id(self, title: str) -> str | None:
        """A list's id by title (one read of the list index a run). None when not read."""
        if self.offline:
            return None
        key = ("list", title.strip().lower())
        if key not in self._c:
            if "list_index" not in self._c:
                if not self._take("the list index"):
                    return None
                self._c["list_index"] = True
            try:
                self._c[key] = self.ctx.crm.list_uuid(title) or ""
            except budget.BudgetExhausted:
                self.counts_left = 0
                self.short["the hour's request budget"] += 1
                return None
        return self._c[key]

    def tag_id(self, title: str) -> str | None:
        if self.offline:
            return None
        key = ("tag", title.strip().lower())
        if key not in self._c:
            if "tag_index" not in self._c:
                if not self._take("the tag index"):
                    return None
                self._c["tag_index"] = True
            try:
                self._c[key] = self.ctx.crm.tag_uuid(title) or ""
            except budget.BudgetExhausted:
                self.counts_left = 0
                self.short["the hour's request budget"] += 1
                return None
        return self._c[key]

    def lead_rows(self) -> list[dict] | None:
        """Every record in a lead status (the search rows carry their task counts), read once a run.
        None when not read."""
        if "lead_rows" not in self._c:
            rows = None
            if self._take("the lead rows"):
                try:
                    rows = leadroutes.status_rows(self.ctx, lanes.LEAD_STATUS_VALUES, cap=int(self.o["max_lead_rows"]))
                    self.counts_left -= len(rows) // 250        # a page is one request
                except budget.BudgetExhausted:
                    self.counts_left = 0
                    self.short["the hour's request budget"] += 1
                    rows = None
            self._c["lead_rows"] = rows
        return self._c["lead_rows"]

    def no_task(self):
        """Leads with no open task now (leadcheck's own reading of the search row), or None."""
        rows = self.lead_rows()
        if rows is None:
            return None
        skip = {s.lower() for s in leadtasks.NO_TASK_NEEDED}
        return sum(1 for r in rows if (leadroutes.stored_status(r) or "").lower() not in skip
                   and _int((r.get("tasks") or {}).get("incomplete")) == 0)

    def board(self) -> tuple[dict, set, bool]:
        """(cards on the board now: record -> card, every record ever on it, whether the board is live).
        Read from the board stage's own memory: the live board's when it is live, else where its plan
        runs placed the cards."""
        def read():
            st = self.stage_of(ftmpipeline.KIND, self.o["board_stage"])
            live = st is not None and ftmpipeline.board_live(self.pol, st.id)
            key = ftmpipeline.FACT if live else ftmpipeline.PLAN_FACT
            return ftmpipeline._facts(key), fact_records(key), live
        return self._cached("board", read)

    def feeds(self) -> list[dict]:
        """Per county feed (a list): records on it, and records created this week and this month that
        carry it. A record that already existed and gained the list is not new here (see the note)."""
        def read():
            monday = self.today - timedelta(days=self.today.weekday())
            first = self.today.replace(day=1)
            tomorrow = (self.today + timedelta(days=1)).isoformat()
            out = []
            for title in self.o["feeds"]:
                lid = self.list_id(title)
                if lid is None:
                    out.append({"feed": title, "total": None, "week": None, "month": None})
                    continue
                if not lid:
                    out.append({"feed": title, "missing": True})
                    continue
                out.append({"feed": title, "total": self.count({"any_lists": [lid]}, title),
                            "week": self.count({"any_lists": [lid], "created": [monday.isoformat(), tomorrow]}, title),
                            "month": self.count({"any_lists": [lid], "created": [first.isoformat(), tomorrow]}, title)})
            return out
        return self._cached("feeds", read)

    # ---- one tab at a time
    def guarded(self, name: str, fn) -> dict:
        if self.cat is None:
            return failed_tab(name, "the KPI catalog (team/kpi.yaml) does not load")
        self._reading = set()
        try:
            t = fn()
            extra = self.issue_note()
            if extra and not t.get("failed"):
                t["note"] = " ".join((str(t.get("note") or "") + extra).split())
            return t
        except kpi.KpiError as exc:
            why = "; ".join(exc.problems[:2])
            self.problem("the %s tab is stopped by the KPI catalog: %s" % (name, why))
            return failed_tab(name, why)
        except budget.BudgetExhausted as exc:
            self.problem("the %s tab stopped: the hour's request budget ran out" % name)
            return failed_tab(name, "the hour's request budget ran out (%s)" % str(exc)[:80])
        except Exception as exc:  # noqa: BLE001  (one tab failing must not stop the others)
            log.warning("kpi tracker tab %s failed: %s", name, str(exc)[:200])
            log.debug("kpi tracker tab %s", name, exc_info=True)
            self.problem("the %s tab could not be built this run; the sheet keeps its last good version (%s)" % (
                name, str(exc)[:120]))
            return failed_tab(name, str(exc)[:160] or type(exc).__name__)

    def problem(self, text: str) -> None:
        if text not in self.problems:
            self.problems.append(text)
        self.ctx.out.problem(text)

    def live_note(self) -> str:
        if not self.short:
            return ""
        return " Some live counts read \"%s\": %s." % (NOT_READ, ", ".join(sorted(self.short)))

    def val(self, v):
        """A live count as a cell: the number, or why it was not read."""
        return NOT_READ if v is None else v

    # ================================================================== tab 1: Scoreboard
    def tab_scoreboard(self) -> dict:
        cat = self.cat
        need = goals.needs(cat)
        risk = cat.share("goal.at_risk_share")
        months = self.months()
        cur = months[-1]
        # The pace counts only the weekdays the log copy holds for the measure's own log: a copy that began
        # mid-month (or not at all) would otherwise read as a slow month and mark everything behind.
        first = self.today.replace(day=1)
        span = {"calls": self.month_days(self._reach_day(activity.CALLS)),
                "state": self.month_days(self._reach_day("property.status.updated")),
                "none": self.month_days(first)}
        section_log = {"Activity": "calls", "Contact": "calls", "Ratios": "calls", "Pipeline": "state", "Speed": "state",
                       "Spend": "none"}
        measures = self.measures()
        # The DataSift KPI tracker's order (2026-11-23): this month first, its pace and mark beside it, the
        # months before it newest first, and the figure it is judged by at the far end.
        older = list(reversed(months[:-1]))
        columns = (["Section", "Monthly", short_label(cur), "Pace this month", "Mark"] + [short_label(w) for w in older]
                   + ["Goal a month", "Benchmark"])
        rows, now_rows = [], []
        marks = {}
        for m in measures:
            if m["kind"] == "now":
                v = m["fn"](None)
                now_rows.append([m["label"], v, m.get("bench", ""), m.get("mark_fn", lambda x: "")(v)])
                continue
            vals = [m["fn"](w) for w in months]
            now_v = vals[-1]
            elapsed, total = span[section_log.get(m["section"], "calls")]
            pace = ""
            if m["kind"] == "count" and m.get("pace", True) and isinstance(now_v, (int, float)):
                if elapsed <= 0:
                    pace = NONE_YET
                else:
                    pace = round(now_v * total / elapsed, 2) if m.get("money") else round(now_v * total / elapsed)
            mark = ""
            if m.get("need") and isinstance(pace, (int, float)):
                mark = kpi.mark(pace, need[m["need"]], risk)
                marks[m["need"]] = mark
            elif m.get("need"):
                marks[m["need"]] = "not judged yet"
            elif m.get("target") is not None and isinstance(now_v, (int, float)):
                mark = kpi.mark(now_v, m["target"], risk, lower_is_better=m.get("lower", False))
            # the month's need worked back from the year's goal, a number in a column of its own (Ty, 2026-11-23:
            # one fact a cell, no sentence of commas beside every row)
            goal = _n1(need[m["need"]]) if m.get("need") else ""
            rows.append([m["section"], m["label"], now_v, pace, mark] + list(reversed(vals[:-1]))
                        + [goal, m.get("bench", "")])
        # what is true right now, and the windows that are not months: set under the table's own columns, so the
        # value sits under this month, the mark under Mark and the goal in the wide column at the end
        pad = [""] * len(older)
        rows.append([])
        rows.append(["Right now and the last %d days" % d90_days(cat)])
        rows.append(["Measure", "Value", "", "Mark"] + pad + ["", "Benchmark"])
        rows += [[label, v, "", mark] + pad + ["", bench] for label, v, bench, mark in now_rows]
        # the last six weeks, Monday to Sunday, newest first, each judged against the week's share of the goal
        weeks = self.weeks()
        newest = list(reversed(weeks))
        rows.append([])
        rows.append(["Last %d weeks (Monday to Sunday)" % len(weeks)])
        rows.append(["Section", "Measure"] + [short_label(w) for w in newest] + ["Weekly goal"])
        cr = contactrate.measure(self.now, self.tz, cat, weeks=len(weeks), include_current=True)
        by_start = {r["start"]: r for r in cr["weeks"]}
        contact_row = ["Contact", "Contact rate (first worked that week and reached since)"] + [
            ("%s of %s (%s%%)" % (by_start[w.first.isoformat()]["reached"], by_start[w.first.isoformat()]["owners"],
                                  _pct(by_start[w.first.isoformat()]["reached"], by_start[w.first.isoformat()]["owners"]))
             if by_start.get(w.first.isoformat()) and by_start[w.first.isoformat()]["owners"] else NONE_YET)
            for w in newest] + [""]
        weekly = [m for m in measures if m["kind"] != "now" and m.get("weekly", True)]
        last_contact = max((i for i, m in enumerate(weekly) if m["section"] == "Contact"), default=len(weekly) - 1)
        for i, m in enumerate(weekly):
            wgoal = _n1(need[m["need"]] * 12.0 / 52.0) if m.get("need") else ""
            rows.append([m["section"], m["label"]] + [m["fn"](w) for w in newest] + [wgoal])
            if i == last_contact:
                rows.append(contact_row)
        # what the Slack lines read from this tab
        lw = [w for w in weeks if not w.current]
        last_week = lw[-1] if lw else None
        self.facts["marks"] = marks
        self.facts["month"] = {"label": cur.label, "leads": len(self.leads(cur)),
                               "contracts": self.changes("contract_statuses", cur),
                               "closed": self.changes("closed_statuses", cur)}
        if last_week is not None:
            self.facts["last_week"] = {"label": last_week.label, "dials": self.calls(last_week)["dials"],
                                       "leads": len(self.leads(last_week)),
                                       "appointments": self.appts(last_week)["total"],
                                       "contracts": self.changes("contract_statuses", last_week)}
        self.facts["texts_released_month"] = self.texts_released(cur)
        note = ("Refreshed %s. Every call, text, phone mark, stage and dollar is read from the connected KPI sources, "
                "each act counted once. Months from the first month the call log copy covers, this month first; this "
                "month's pace is the month so far over the weekdays gone that the log copy holds (today counted by the "
                "share of business hours past), times the month's weekdays. Marks judge the pace against the need a "
                "month worked back from the year's goal (the Goals tab, else team/kpi.yaml); with no weekday on record "
                "yet, the mark reads not judged yet. A week's goal is the month's need times 12, over 52. %s %s %s %s "
                "Every lead is credited to something: a caller when the log holds a conversation of ours before it, a "
                "call-in or a text reply when that was the conversation, else texts when the autopilot texted the owner "
                "or a number of theirs was marked correct outside a call in the 30 days before, else the autopilot when "
                "our own key set the status, else DataSift automation when its system user did (a dialer disposition or "
                "a sequence), else no source found. "
                "Outreach texts are the texts sent before any reply; a reply is counted once, the first in its thread. "
                "Talk time is the mean length of a conversation call. %s" % (
                    self.refreshed_words(), " ".join(str(cat.definition("opportunity").get("says") or "").split()),
                    self.texts_note(), self.offers_note(), self.revenue_note(), footnote(cat, USES)))
        if span["calls"][0] <= 0:
            note += " No business hour of this month is on record yet, so its pace is not judged."
        reach = self._reach_day(activity.CALLS)
        if reach is not None and (reach > months[0].first or reach > weeks[0].first):
            note += (" The call log copy starts %s, so a month or week that begins before that day counts from it, and "
                     "reads short." % _nice(reach))
        return tab("Scoreboard", columns, rows, note + self.live_note())

    def month_days(self, since: date | None) -> tuple[float, int]:
        """(weekdays of this month behind us that the log copy holds, from `since`, today counted by the
        share of business hours gone; the month's weekdays). None for `since` means the copy holds nothing."""
        first = self.today.replace(day=1)
        total = kpi.weekdays(first, _next_month(first))
        if since is None or since > self.today:
            return 0.0, total
        start = max(first, since)
        done = float(kpi.weekdays(start, self.today))
        if self.today.weekday() < 5:
            open_, close = self.pol.hours
            m = self.local.hour * 60 + self.local.minute
            done += max(0.0, min(1.0, (m - open_) / float(max(1, close - open_))))
        return done, total

    def measures(self) -> list[dict]:
        cat = self.cat
        c = self.calls

        def cost_per(count_fn):
            def fn(w):
                typed = cat.monthly_costs()
                if not typed:
                    return GAP
                if w is None or w.current:
                    return NONE_YET
                n = count_fn(w)
                return round((sum(self.spend(w).values()) + sum(typed.values())) / n) if n else NONE_YET
            return fn

        lead_n = lambda w: len(self.leads(w))                                      # noqa: E731
        contracts_n = lambda w: self.changes("contract_statuses", w)                # noqa: E731
        d90 = self.since_days(int(cat.value("goal.deal_window_days")))

        def leads_per_contract(_):
            return _ratio(len(self.leads(d90)), self.changes("contract_statuses", d90))

        def close_rate(_):
            return _pct(self.changes("closed_statuses", d90), self.changes("contract_statuses", d90))

        return [
            {"section": "Activity", "label": "Dials", "fn": lambda w: c(w)["dials"], "kind": "count", "need": "dials",
             "bench": bench(cat, "caller.dials_per_day", "per caller")},
            {"section": "Activity", "label": "Owners dialed", "fn": lambda w: c(w)["owners"], "kind": "count", "pace": False},
            {"section": "Activity", "label": "Answered", "fn": lambda w: c(w)["answered"], "kind": "count",
             "bench": "normal is %s" % cat.text("alarm.answer_rate_normal")},
            {"section": "Activity", "label": "Conversations", "fn": lambda w: c(w)["conversations"], "kind": "count",
             "need": "conversations", "bench": bench(cat, "caller.conversations_per_day", "per caller")},
            {"section": "Activity", "label": "Avg talk time per conversation (m:ss)", "fn": self.avg_talk, "kind": "ratio"},
            {"section": "Activity", "label": "Outreach texts sent (before a reply)", "fn": self.outreach_texts,
             "kind": "count"},
            {"section": "Activity", "label": "Initial text replies (first reply in a thread)", "fn": self.first_replies,
             "kind": "count" if self.texting() else "gap", "pace": False},
            {"section": "Contact", "label": "Correct numbers (all channels)", "fn": lambda w: self.correct(w)["total"],
             "kind": "count"},
            {"section": "Contact", "label": "Dials per correct number",
             "fn": lambda w: _ratio(c(w)["dials"], self.correct(w)["total"]), "kind": "ratio",
             "bench": cat.cite("funnel.dials_per_correct_number"), "target": cat.value("funnel.dials_per_correct_number"),
             "lower": True},
            {"section": "Contact", "label": "Texts per correct number", "fn": self.texts_per_correct, "kind": "ratio"},
            {"section": "Contact", "label": "Texts per reply", "fn": self.texts_per_reply, "kind": "ratio"},
            {"section": "Contact", "label": "Owners reached by call", "fn": lambda w: self.reached_split(w)["call"],
             "kind": "count", "pace": False},
            {"section": "Contact", "label": "Owners reached by text", "fn": lambda w: self.reached_split(w)["text"],
             "kind": "count", "pace": False},
            {"section": "Pipeline", "label": "New leads", "fn": lead_n, "kind": "count", "need": "leads",
             "bench": bench(cat, "caller.opportunities_per_day", "per caller")},
            {"section": "Pipeline", "label": "Leads from a caller", "fn": lambda w: self.lead_sources(w)["caller"],
             "kind": "count"},
            {"section": "Pipeline", "label": "Leads from texts", "fn": lambda w: self.lead_sources(w)["texts"],
             "kind": "count"},
            {"section": "Pipeline", "label": "Leads from a call-in", "fn": lambda w: self.lead_sources(w)["call-in"],
             "kind": "count"},
            {"section": "Pipeline", "label": "Leads set by the autopilot", "fn": lambda w: self.lead_sources(w)["autopilot"],
             "kind": "count"},
            {"section": "Pipeline", "label": "Leads set by DataSift automation",
             "fn": lambda w: self.lead_sources(w)["automation"], "kind": "count"},
            {"section": "Pipeline", "label": "Leads with no source found", "fn": lambda w: self.lead_sources(w)["none"],
             "kind": "count"},
            {"section": "Pipeline", "label": "Qualified leads", "fn": lambda w: self.changes("qualified_statuses", w),
             "kind": "count", "bench": NO_FIGURE},
            {"section": "Pipeline", "label": "Appointments", "fn": lambda w: self.appts(w)["total"], "kind": "count",
             "bench": NO_FIGURE},
            {"section": "Pipeline", "label": "Offers made", "fn": self.offers, "kind": "count", "pace": False,
             "bench": bench(cat, "role.closer_offers_per_day", "per closer")},
            {"section": "Pipeline", "label": "Contracts signed", "fn": contracts_n, "kind": "count", "need": "contracts"},
            {"section": "Pipeline", "label": "Deals closed", "fn": lambda w: self.changes("closed_statuses", w),
             "kind": "count", "need": "deals"},
            {"section": "Pipeline", "label": "Revenue closed ($)", "fn": self.revenue, "kind": "count", "money": True},
            {"section": "Ratios", "label": "Dials per lead", "fn": lambda w: _ratio(c(w)["dials"], lead_n(w)), "kind": "ratio",
             "bench": cat.cite("funnel.dials_per_lead"), "target": cat.high("funnel.dials_per_lead"), "lower": True},
            {"section": "Ratios", "label": "Conversations per 1,000 dials",
             "fn": lambda w: _ratio(1000 * c(w)["conversations"], c(w)["dials"], 0), "kind": "ratio"},
            {"section": "Ratios", "label": "Text replies per lead from texts", "fn": self.replies_per_text_lead,
             "kind": "ratio"},
            {"section": "Ratios", "label": "Leads per contract, last %d days" % d90_days(cat), "fn": leads_per_contract,
             "kind": "now", "bench": cat.cite("funnel.leads_per_deal")},
            {"section": "Ratios", "label": "Contracts that close, last %d days (%%)" % d90_days(cat), "fn": close_rate,
             "kind": "now", "bench": cat.cite("funnel.contracts_that_close"),
             "mark_fn": lambda v: kpi.mark(v, cat.value("funnel.contracts_that_close"), cat.share("goal.at_risk_share"))
             if isinstance(v, (int, float)) else ""},
            {"section": "Pipeline", "label": "Fees under contract, expected ($)", "fn": lambda _: self.fees_open(),
             "kind": "now", "bench": "signed deals on the Goals tab"},
            {"section": "Speed", "label": "Speed to lead (median minutes)", "fn": lambda w: median_minutes(self.speed(w)[0]),
             "kind": "ratio", "bench": NO_FIGURE},
            {"section": "Speed", "label": "Leads with no next task (now)", "fn": lambda _: self.val(self.no_task()),
             "kind": "now", "bench": NO_FIGURE},
            {"section": "Spend", "label": "Paid vendor spend ($)", "fn": lambda w: round(sum(self.spend(w).values()), 2),
             "kind": "count", "money": True},
            {"section": "Spend", "label": "Mail ($)", "fn": lambda w: self.spend(w)["mail"], "kind": "count",
             "money": True},
            {"section": "Spend", "label": "Skip trace and phone scoring ($)", "fn": lambda w: self.spend(w)["data"],
             "kind": "count", "money": True},
            {"section": "Spend", "label": "Research ($)", "fn": lambda w: self.spend(w)["research"], "kind": "count",
             "money": True},
            {"section": "Spend", "label": "Cost per lead ($)", "fn": cost_per(lead_n), "kind": "ratio",
             "weekly": False},
            {"section": "Spend", "label": "Cost per contract ($)", "fn": cost_per(contracts_n), "kind": "ratio",
             "bench": cat.cite("owner.cost_per_deal"), "weekly": False},
        ]

    def refreshed_words(self) -> str:
        return "%s at %s %s, the %s run" % (self.local.strftime("%A, %B %d").replace(" 0", " "), _clock(self.local),
                                            self.local.tzname() or "", self.run)

    # ================================================================== tab 2: Today
    def caller_table(self, w: Win) -> tuple[list[list], dict]:
        """Rows per caller for one window (Today and the Callers tracker share this), and the team's row."""
        rows_by = caller_rows(self.acts(w, "calls"), self.conv_s)
        extras = self.extras_by_caller(w)
        credited, unplaced = self.credit(w)
        who = (set(rows_by) | set(extras) | set(credited))
        who = {x for x in who if x.strip().lower() not in self.not_callers}
        out, team = [], Counter()

        def clock(t):
            return t.astimezone(self.zone).strftime("%I:%M %p").lstrip("0") if t else ""

        for name in sorted(who - {UNKNOWN, NOT_TIED}, key=lambda n: (-(rows_by.get(n) or {}).get("dials", 0), n)):
            r = rows_by.get(name) or {}
            first, last = r.get("first"), r.get("last")
            hours = round((last - first).total_seconds() / 3600.0, 1) if first and last else ""
            conv = _int(r.get("conversations")) + extras.get(name, 0)
            out.append([_first_name(name), clock(first), clock(last), hours,
                        _int(r.get("dials")), _int(r.get("owners")), _int(r.get("numbers")), _int(r.get("answered")), conv,
                        round(_int(r.get("talk_s")) / 60.0), NONE_YET, NONE_YET, NONE_YET, NONE_YET, _int(r.get("noanswer")),
                        credited.get(name, 0), NONE_YET])
            team.update({"hours": hours if isinstance(hours, float) else 0, "dials": _int(r.get("dials")),
                         "answered": _int(r.get("answered")), "conversations": conv,
                         "talk": round(_int(r.get("talk_s")) / 60.0), "noanswer": _int(r.get("noanswer")),
                         "numbers": _int(r.get("numbers"))})
        r = rows_by.get(UNKNOWN) or {}
        loose_dials = _int(r.get("dials"))
        loose_conv = _int(r.get("conversations")) + extras.get(NOT_TIED, 0) + extras.get(UNKNOWN, 0)
        loose_noans = _int(r.get("noanswer"))
        if loose_dials or loose_conv or loose_noans:
            out.append([NOT_TIED, "", "", "", loose_dials, _int(r.get("owners")), _int(r.get("numbers")), _int(r.get("answered")),
                        loose_conv, round(_int(r.get("talk_s")) / 60.0), NONE_YET, NONE_YET, NONE_YET, NONE_YET, loose_noans,
                        "", NONE_YET])
            team.update({"dials": loose_dials, "answered": _int(r.get("answered")), "conversations": loose_conv,
                         "talk": round(_int(r.get("talk_s")) / 60.0), "noanswer": loose_noans, "numbers": _int(r.get("numbers"))})
        calls = self.calls(w)
        corr = self.correct(w)
        mk = self.marks(w)
        firsts = [x["first"] for x in rows_by.values()]
        lasts = [x["last"] for x in rows_by.values()]
        team_row = ["Team", min(firsts).astimezone(self.zone).strftime("%I:%M %p").lstrip("0") if firsts else "",
                    max(lasts).astimezone(self.zone).strftime("%I:%M %p").lstrip("0") if lasts else "",
                    round(team["hours"], 1), calls["dials"], calls["owners"], team["numbers"], calls["answered"],
                    calls["conversations"], calls["talk_min"], corr["total"], mk["Wrong"], mk["Dead"], mk["DNC"],
                    team["noanswer"], len(self.leads(w)), self.appts(w)["total"]]
        facts = {"label": w.label, "first": w.first.isoformat(), "dials": calls["dials"], "conversations": calls["conversations"],
                 "correct": corr["total"], "leads": len(self.leads(w)), "unplaced": unplaced,
                 "credited": {_first_name(k): v for k, v in credited.items() if k.strip().lower() not in self.not_callers},
                 "callers": [(row[0], row[4]) for row in out if row[0] != NOT_TIED]}
        return out + [team_row], facts

    CALLER_COLUMNS = ["Caller", "First dial", "Last dial", "Hours on the dialer", "Dials", "Owners dialed",
                      "Numbers dialed", "Answered", "Conversations", "Talk minutes", "Correct numbers", "Wrong", "Dead",
                      "DNC", "No answer", "Leads credited", "Appointments set"]

    def tab_today(self) -> dict:
        cat = self.cat
        w = self.day_win
        table, facts = self.caller_table(w)
        self.facts["day"] = facts
        # Kendrick Eid, 2026-11-23: "Ratios on funnel up top". The day's funnel first, each step against the one
        # before it, then the callers, then the lead manager.
        rows = self.funnel_rows(w)
        rows += [[], ["Callers on %s" % w.label], list(self.CALLER_COLUMNS)] + list(table)
        # the lead manager's day
        waits, never, unknown = self.speed(w)
        tasks = self.lead_tasks()
        self.facts["speed_median"] = median_minutes(waits) if waits else None
        self.facts["no_task"] = self.no_task()
        att = self.lead_attempts(w)
        lm = [["New leads in", len(self.leads(w)), ""],
              ["Leads not called yet", never, ""],
              ["Speed to lead (median minutes)", median_minutes(waits), NO_FIGURE],
              ["Leads with no next task (now)", self.val(self.no_task()), NO_FIGURE],
              ["Lead attempts (calls and texts to lead owners)", att["attempts"],
               bench(cat, "role.lead_manager_attempts_per_day", "per lead manager")],
              ["Conversations with leads", att["conversations"], ""],
              ["Lead tasks due today (log copy)", self.val(tasks.get("due_today")), ""],
              ["Lead tasks overdue (log copy)", self.val(tasks.get("overdue")), "none"],
              ["Outreach texts sent (before a reply)", self.outreach_texts(w), ""],
              ["Initial text replies", self.first_replies(w), ""]]
        # the benchmark in the funnel's own Benchmark column (the fourth), where the text has room
        rows += [[], ["Lead manager on %s" % w.label], ["Measure", "Value", "", "Benchmark"]]
        rows += [[label, v, "", b] for label, v, b in lm]
        # Kendrick Eid, 2026-11-23: the pipelines block is off Today (the board's counts are on First to Market)
        note = ("%s. %s (each dial's own moment); numbers dialed are distinct numbers per caller. A phone mark (correct, "
                "wrong, dead, do not call) does not say who set it, so marks are the team's and a caller's cells read "
                "\"-\". Appointments are the team's too: a task does not name the caller who set it. On a caller's row, "
                "leads are the leads credited to that caller; on the team row, every new lead. A text reply or a call "
                "back goes to the caller named on it, else to the one caller the daily capture holds for the day, else "
                "to no caller. Lead tasks are read from the log copy, so a task made before the copy starts is not seen. "
                "%s" % ("The %s final" % _day_word(w.first) if not w.current else "Today so far, refreshed %s"
                        % _clock(self.local), "Hours on the dialer run from the first dial to the last", self.texts_note()))
        if self.calls(w)["length_unknown"]:
            note += (" %s no length from any source, so none of them is counted as a conversation."
                     % ("1 answered call carries" if self.calls(w)["length_unknown"] == 1 else
                        "%d answered calls carry" % self.calls(w)["length_unknown"]))
        if unknown:
            note += " %d new lead(s) are not timed: the speed job has not seen their owner yet." % unknown
        return tab("Today", self.FUNNEL_COLUMNS, rows, note + self.live_note())

    FUNNEL_COLUMNS = ["Funnel", "Count", "Rate", "Benchmark"]

    def funnel_rows(self, w: Win) -> list[list]:
        """The window's funnel, each step with its rate against the step before: dials, answered (of dials),
        conversations (of answered), correct numbers and new leads (dials each)."""
        cat = self.cat
        c = self.calls(w)
        corr, leads = self.correct(w)["total"], len(self.leads(w))

        def share(n, d, of):
            v = _pct(n, d, 0)
            return ("%s%% of %s" % (kpi.num(v), of)) if isinstance(v, (int, float)) else NONE_YET

        def each(n, d):
            v = _ratio(d, n)
            return ("1 per %s dials" % kpi.num(v)) if isinstance(v, (int, float)) else NONE_YET

        return [["Dials", c["dials"], "", bench(cat, "caller.dials_per_day", "per caller")],
                ["Answered", c["answered"], share(c["answered"], c["dials"], "dials"),
                 "normal is %s" % cat.text("alarm.answer_rate_normal")],
                ["Conversations", c["conversations"], share(c["conversations"], c["answered"], "answered"),
                 bench(cat, "caller.conversations_per_day", "per caller")],
                ["Correct numbers", corr, each(corr, c["dials"]), cat.cite("funnel.dials_per_correct_number")],
                ["New leads", leads, each(leads, c["dials"]), cat.cite("funnel.dials_per_lead")]]

    def lead_tasks(self) -> dict:
        """Open tasks on the leads, from the log copy: {} when the lead rows were not read, because a
        count over every record would be read as the lead manager's."""
        rows = self.lead_rows()
        if rows is None:
            return {}
        return self._cached("lead_tasks", lambda: open_tasks(self.today, self.tz, {r.get("uuid") or "" for r in rows}))

    def pipeline_presets(self) -> list[str]:
        out = []
        for sid in self.o["pipeline_stages"]:
            st = (getattr(self.pol, "stages", None) or {}).get(sid)
            if st is not None:
                out += [p for p in st.presets if p not in out]
        return out

    def lead_attempts(self, w: Win) -> dict:
        """Our calls and the texts sent to owners in the lead lane, and the conversations with them, joined
        to the nightly owner-lane map. The map says where the owner sits today, so a call made before the
        owner became a lead counts too: a floor on the lead manager's day, said in the note."""
        def read():
            leads = {o for o, (lane, _) in self.lanes_map().items() if lane == "lead"}
            n = sum(1 for a in self.acts(w, "calls") if a.direction == "out" and not outside_call(a)
                    and self.party(a) in leads)
            conv = sum(1 for x in self.conv(w)["items"] if x["party"] in leads)
            return {"attempts": n, "conversations": conv}
        return self._cached(("lead_attempts", w.start, w.end), read)

    # ================================================================== tab 3: Callers
    def tab_callers(self) -> dict:
        cat = self.cat
        days = []
        d = self.today
        include_today = self.run != "morning" and self.today.weekday() in self.days
        if not include_today:
            d = last_calling_day(self.today, self.days)
        while len(days) < int(self.o["caller_days"]):
            if d.weekday() in self.days:
                days.append(d)
            d -= timedelta(days=1)
        # A day before the call log copy starts would read as a day of no calls. It is left off instead.
        reach = self._reach_day(activity.CALLS)
        days = [x for x in days if reach is not None and x >= reach]
        rows = []
        columns = ["Day"] + self.CALLER_COLUMNS
        for d in days:
            w = self.day(d, current=(d == self.today))
            table, _ = self.caller_table(w)
            for r in table:
                rows.append([_nice(d)] + r)
        # judged on 90 days, the goal sheet's own lines
        judge = int(cat.value("caller.judge_days"))
        d90 = self.since_days(judge)
        jw = Win("Last %d days" % judge, d90.start, self.today_start, d90.first, self.today, False)
        credit, _ = self.credit(jw)
        _, judged = goals.caller_lines(self.ctx, cat, self.today, dict(credit))
        # Kendrick Eid, 2026-11-23: "On Judged the last 90 days add ratios". Each caller's own funnel over the same
        # window beside the floors: answered of dials, dials a conversation, conversations a lead, dials a lead.
        per = caller_rows_by_day(self.acts(jw, "calls"), self.conv_s, self.zone)
        extras = self.extras_by_caller(jw)
        rows += [[], ["Judged on the last %d days" % judge],
                 ["Caller", "Calling days", "Dials a day", "Floor", "Mark", "Conversations a day", "Floor", "Mark",
                  "Leads a day", "Floor", "Mark", "Answered (%)", "Dials per conversation", "Dials per correct number",
                  "Conversations per lead", "Dials per lead"]]
        team = Counter()
        for r in judged:
            if r["caller"].strip().lower() in self.not_callers:
                continue
            got = per.get(r["caller"]) or {}
            dials, answered = _int(got.get("dials")), _int(got.get("answered"))
            conv = _int(got.get("conversations")) + extras.get(r["caller"], 0)
            won = credit.get(r["caller"], 0)
            team.update({"dials": dials, "answered": answered, "conversations": conv, "leads": won})
            rows.append([_first_name(r["caller"]), r["calling_days"], r["dials_per_day"], cat.text("caller.dials_per_day"),
                         r["marks"][0], r["conversations_per_day"], cat.text("caller.conversations_per_day"), r["marks"][1],
                         r["opportunities_per_day"], cat.text("caller.opportunities_per_day"), r["marks"][2],
                         _pct(answered, dials), _ratio(dials, conv), NONE_YET, _ratio(conv, won), _ratio(dials, won)])
        calls_j = self.calls(jw)
        dpc = _ratio(calls_j["dials"], self.correct(jw)["total"])
        rows.append(["Team", "", "", "", "", "", "", "", "", "", "", _pct(calls_j["answered"], calls_j["dials"]),
                     _ratio(calls_j["dials"], calls_j["conversations"]), dpc,
                     _ratio(calls_j["conversations"], len(self.leads(jw))), _ratio(calls_j["dials"], len(self.leads(jw)))])
        # by pipeline this week
        wk = self.week(self.today - timedelta(days=self.today.weekday()))
        rows += [[], ["By pipeline in the %s (the owner's lane today)" % (wk.label[:1].lower() + wk.label[1:])],
                 ["Lane", "Dials", "Owners dialed", "Conversations", "Correct numbers"]]
        for lane, v in self.by_lane(wk):
            rows.append([lane_label(lane) if lane != "none" else "Not on the lane map", v["dials"], len(v["owners"]),
                         v["conversations"], v["correct"]])
        sent = self.texts_by_person(wk)
        if sent is not None:
            rows += [[], ["Texts sent by person in the %s" % (wk.label[:1].lower() + wk.label[1:])], ["Person", "Texts sent"]]
            for name, n in sent:
                rows.append([name, n])
            if not sent:
                rows.append(["no text sent this week", 0])
        rows += [[], ["Not tracked yet"], ["By attempt (Day 1 to Day 5 and Exhausted)", GAP],
                 ["Contracts credited to a caller", GAP]]
        if sent is None:
            rows.append(["Texts a caller sent", GAP])
        note = ("One row per caller per business day for the last %d business day(s) the call log copy holds, newest "
                "first, and a team row for each day. Today's rows are provisional until the evening run. Phone marks "
                "and appointments are the team's (a mark does not say who set it). On a caller's row, leads are the "
                "leads credited to that caller; on the team row, every new lead. By attempt is not tracked yet: the "
                "attempt numbers exist (the attempt keeper, the attempt log) but nothing tabulates calls by them. "
                "Contracts credited to a caller need the lead ledger to follow a lead to its contract. %s" % (
                    len(days), self.texts_note()))
        if not days:
            note += " The call log copy holds no call yet."
        return tab("Callers", columns, rows, note + self.live_note())

    def texts_by_person(self, w: Win) -> list[tuple[str, int]] | None:
        """Texts sent in the window by the person the texting tool names, most first; None while no texting
        tool is connected."""
        if not self.texting():
            return None
        got: Counter = Counter()
        for a in self.acts(w, "calls"):
            if ks.METRIC_TEST["texts_sent"](a):
                got[_first_name(person_name(a.person_ref)) or "not named by the tool"] += 1
        return sorted(got.items(), key=lambda kv: (-kv[1], kv[0]))

    def by_lane(self, w: Win) -> list[tuple[str, dict]]:
        out: dict = defaultdict(lambda: {"dials": 0, "owners": set(), "conversations": 0, "correct": 0})
        for a in our_dials(self.acts(w, "calls")):
            r = out[self.owner_lane(a.owner) or "none"]
            r["dials"] += 1
            r["owners"].add(a.owner)
        for x in self.conv(w)["items"]:
            out[self.owner_lane(x["party"]) or "none"]["conversations"] += 1
        seen = set()
        for a in sorted(self.acts(w, "phone"), key=lambda x: x.at):
            if a.outcome in CORRECT:
                k = (self.party(a), a.phone)
                if k not in seen:
                    seen.add(k)
                    out[self.owner_lane(self.party(a)) or "none"]["correct"] += 1
        order = list(lanes.ORDER) + ["none"]
        return sorted(out.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)

    # ================================================================== tab: Texts
    def text_measures(self) -> list[tuple]:
        """(section, label, fn) for the Texts tab, month and week alike."""
        led = self.text_ledger

        def rate(w):
            r, n = self.first_replies(w), led(w)["owners"]
            if r == GAP:
                return GAP
            return _pct(r, n) if isinstance(n, int) else NONE_YET

        def gap_or(v):
            return "could not be read" if v is None else v

        return [
            ("Outreach", "Outreach texts sent (before a reply)", self.outreach_texts),
            ("Outreach", "Owners texted", lambda w: gap_or(led(w)["owners"])),
            ("Outreach", "Numbers texted", lambda w: gap_or(led(w)["numbers"])),
            ("Outreach", "Opening texts (texts 1 to 3)", lambda w: gap_or(led(w)["opening"])),
            ("Outreach", "Follow-up texts", lambda w: gap_or(led(w)["followup"])),
            ("Replies", "Initial text replies (first reply in a thread)", self.first_replies),
            ("Replies", "Reply rate (% of owners texted)", rate),
            ("Replies", "Numbers confirmed by a text reply", lambda w: self.correct_split(w)["text"]),
            ("Replies", "Wrong numbers marked on a text reply", lambda w: self.text_marks(w)["Wrong"]),
            ("Replies", "Do not contact marked on a text reply", lambda w: self.text_marks(w)["DNC"]),
            ("Results", "Owners reached by text", lambda w: self.reached_split(w)["text"]),
            ("Results", "Leads from texts", lambda w: self.lead_sources(w)["texts"]),
            ("Results", "Texts per correct number", self.texts_per_correct),
            ("Results", "Texts per reply", self.texts_per_reply),
            ("Results", "Text replies per lead from texts", self.replies_per_text_lead),
        ]

    def tab_texts(self) -> dict:
        """Kendrick Eid, 2026-11-23: "Add a tab for text metrics". What the autopilot texted, what came back and
        what it made, by month, this month by lane, and by week. The texts come from the autopilot's own
        ledger (Smarter Contact sends never reach DataSift's log); replies come from a texting tool that
        reports them, and read not tracked yet until one does."""
        months = self.months()
        cur = months[-1]
        older = list(reversed(months[:-1]))
        wins = [cur] + older
        measures = self.text_measures()
        columns = ["Section", "Monthly"] + [short_label(w) for w in wins]
        rows = [[sec, label] + [fn(w) for w in wins] for sec, label, fn in measures]
        rows += [[], ["This month by lane"],
                 ["Lane", "Texts sent", "Owners texted", "Confirmed by a text reply", "Leads from texts"]]
        lane_rows = self.texts_by_lane(cur)
        rows += lane_rows or [["no text released this month", 0, 0, 0, 0]]
        weeks = self.weeks()
        newest = list(reversed(weeks))
        rows += [[], ["Last %d weeks (Monday to Sunday)" % len(weeks)],
                 ["Section", "Measure"] + [short_label(w) for w in newest]]
        rows += [[sec, label] + [fn(w) for w in newest] for sec, label, fn in measures]
        note = ("Outreach texts are the texts sent before any reply: the ones the autopilot released into the texting "
                "tool, read from its own ledger, or once a texting tool reports its sends, every text it sent before a "
                "number's first reply. A reply is counted once, the first in its thread. A number confirmed, marked "
                "wrong or marked do not contact on a text reply is the texting app's own mark (our key, outside the "
                "brain's stages); before 2026-11-15 the log does not tell its marks from a person's, so earlier "
                "weeks read low. A lead from texts is one with no call of ours before it whose owner the autopilot "
                "texted, or whose number was marked correct outside any call, in the 30 days before. The autopilot's "
                "own text ledger starts 2026-11-18. %s" % self.texts_note())
        if not self.texting():
            note += (" Replies, the reply rate, texts per reply and replies per lead read not tracked yet until the "
                     "texting app reports its replies to the tracker.")
        return tab("Texts", columns, rows, note + self.live_note())

    # ================================================================== tab 4: Leads
    def lead_day_windows(self) -> list[Win]:
        out = []
        d = self.today if (self.run != "morning" and self.today.weekday() in self.days) else last_calling_day(self.today, self.days)
        while len(out) < int(self.o["lead_days"]):
            if d.weekday() in self.days:
                out.insert(0, self.day(d, current=(d == self.today)))
            d -= timedelta(days=1)
        return out

    def tab_leads(self) -> dict:
        cat = self.cat
        days = self.lead_day_windows()
        columns = ["Measure"] + [w.label for w in days] + ["Benchmark"]
        rows = [
            ["New leads in"] + [len(self.leads(w)) for w in days] + [""],
            ["Lead attempts (calls and texts to lead owners)"] + [self.lead_attempts(w)["attempts"] for w in days]
            + [bench(cat, "role.lead_manager_attempts_per_day", "per lead manager")],
            ["Conversations with leads"] + [self.lead_attempts(w)["conversations"] for w in days] + [""],
            ["Qualified"] + [self.changes("qualified_statuses", w) for w in days] + [NO_FIGURE],
            ["Pass-offs (appointments set)"] + [self.appts(w)["total"] for w in days]
            + [bench(cat, "role.lead_manager_passoffs_per_day", "per lead manager")],
            ["Speed to lead, median minutes"] + [median_minutes(self.speed(w)[0]) for w in days] + [NO_FIGURE],
        ]
        quiet = self.quiet_leads()
        tasks = self.lead_tasks()
        rows += [[], ["Now"], ["Measure", "Value", "Benchmark"],
                 ["Leads with no next task", self.val(self.no_task()), NO_FIGURE],
                 ["Lead tasks overdue (log copy)", self.val(tasks.get("overdue")), "0"],
                 ["Hot leads gone quiet", self.val(quiet.get("hot")), "a talk every %s" % cat.text("cadence.hot_days")],
                 ["Warm leads gone quiet", self.val(quiet.get("warm")), "a talk every %s" % cat.text("cadence.warm_days")],
                 ["Cold leads gone quiet", self.val(quiet.get("cold")), "a talk every %s" % cat.text("cadence.cold_days")]]
        # the lead ledger
        rows += [[], ["Lead ledger this month and last"], self.LEDGER_COLUMNS]
        rows += self.lead_ledger()
        # the pipeline now
        rows += [[], ["Leads by status now"], ["Status", "Leads"]]
        lr = self.lead_rows()
        if lr is None:
            rows.append(["(the lead rows were not read)", NOT_READ])
        else:
            for s, n in Counter(status_word(leadroutes.stored_status(r)) or "No status" for r in lr).most_common():
                rows.append([s, n])
        rows += [[], ["Leads by board column now (the last card move in the log copy)"], ["Column", "Leads"]]
        for col, n in self.lead_columns().most_common():
            rows.append([col, n])
        note = ("A lead is a record entering a lead status from outside one (team/kpi.yaml). Lead attempts join the "
                "day's calls to the nightly owner-lane map, which says where an owner sits today, so a call made "
                "before the owner became a lead counts too. Qualified is a move to %s (decision F9). Pass-offs are "
                "appointments set until the acquisitions columns are named (F9). Gone quiet means no conversation "
                "inside the catalog's cadence (cadence.hot_days, warm_days, cold_days). The ledger shows record links "
                "only%s; first offer and offer accepted are not tracked yet (plan 2.10)." % (
                    ", ".join(self.o["qualified_statuses"]),
                    " and a street when the brain already knows it" if self.o["ledger_streets"] else ""))
        if quiet.get("unknown"):
            note += " %d lead(s) were not judged for quiet: the brain does not know their owner yet." % quiet["unknown"]
        return tab("Leads", columns, rows, note + self.live_note())

    LEDGER_COLUMNS = ["Record", "Added to DataSift", "Became a lead", "Channel", "Source", "Caller credited", "Qualified",
                      "First offer", "Offer accepted", "Under contract", "Closed or lost", "Days lead to contract",
                      "Status now"]

    def link(self, record: str) -> str:
        return str(self.o["record_url"]) % record

    def _status_after(self, record: str, after: datetime, statuses) -> datetime | None:
        want = {str(s).strip().lower() for s in statuses}
        for e in activity.events("property.status.updated", resource=record, since=after):
            if str(e["data"].get("to") or "").strip().lower() in want:
                return e["when"]
        return None

    def _street(self, record: str) -> str:
        if not self.o["ledger_streets"]:
            return ""
        info = ledger.fact(record, "lead_owner")
        return str(info.get("street") or "") if isinstance(info, dict) else ""

    def lead_ledger(self) -> list[list]:
        """One row per lead this month and last, each step dated from the log copy. The channel, source
        and caller are the deal sources' own reading (dealsource.origin and source_of), run the day a
        record becomes a lead instead of only at closing."""
        first = (self.today.replace(day=1) - timedelta(days=1)).replace(day=1)
        tracking = self.tracking()
        tags = self.researched_tags()
        out = []
        both = Win("this month and last", kpi.day_start(first, self.tz), self.now, first, self.today + timedelta(days=1), True)
        for lead in self.leads(both)[-int(self.o["ledger_rows"]):]:
            u, at = lead["record"], lead["when"]
            owner = self.owner_of(u)
            org = dealsource.origin(self.cat, owner, at, tracking) if owner else {"channel": "", "caller": ""}
            earlier = len(dealsource.lead_moments(u, until=at))
            src = dealsource.source_of(u, at, earlier, tags)
            made = activity.events("property.created", resource=u, limit=1)
            q = self._status_after(u, at, self.o["qualified_statuses"])
            k = self._status_after(u, at, self.o["contract_statuses"])
            end = self._status_after(u, at, list(self.o["closed_statuses"]) + list(self.o["lost_statuses"]))
            last = activity.events("property.status.updated", resource=u)
            status_now = status_word(last[-1]["data"].get("to")) if last else ""
            link = self.link(u)
            street = self._street(u)
            out.append([("%s (%s)" % (street, link)) if street else link,
                        _nice(made[0]["when"].astimezone(self.zone).date()) if made else "before the log copy",
                        _nice(at.astimezone(self.zone).date()), org.get("channel") or ("owner not known yet" if not owner else "not in the log"),
                        src, _first_name(org.get("caller") or ""), _nice(q.astimezone(self.zone).date()) if q else "",
                        GAP, GAP, _nice(k.astimezone(self.zone).date()) if k else "",
                        _nice(end.astimezone(self.zone).date()) if end else "",
                        (k - at).days if k else "", status_now])
        if not out:
            out.append(["no lead this month or last"])
        return out

    def quiet_leads(self) -> dict:
        """Hot, warm and cold leads with no real conversation inside the catalog's cadence, the lead
        clock's own reading of a conversation (leadclock.last_conversation)."""
        rows = self.lead_rows()
        if rows is None:
            return {}
        cad = {"hot lead": ("hot", self.cat.value("cadence.hot_days")), "warm lead": ("warm", self.cat.value("cadence.warm_days")),
               "cold lead": ("cold", self.cat.value("cadence.cold_days"))}
        out: Counter = Counter({"hot": 0, "warm": 0, "cold": 0})
        for r in rows:
            k = cad.get((leadroutes.stored_status(r) or "").lower())
            if not k:
                continue
            owner = ((r.get("owner") or {}).get("uuid") or "") or self.owner_of(r.get("uuid") or "")
            if not owner:
                out["unknown"] += 1
                continue
            when, _ = leadclock.last_conversation(owner, r.get("uuid") or "", seconds=self.conv_s)
            if when is None or (self.now - when).total_seconds() / 86400.0 > float(k[1]):
                out[k[0]] += 1
        return dict(out)

    def lead_columns(self) -> Counter:
        from ..workflows import kpi_report
        titles = None
        out: Counter = Counter()
        for r in self.lead_rows() or []:
            e = leadroutes.last_card_event(r.get("uuid") or "")
            if e is None:
                out["no card in the log copy"] += 1
                continue
            col = str(e["data"].get("column") or "")
            if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-", col, re.I):
                titles = kpi_report._column_titles() if titles is None else titles
                col = titles.get(col.lower(), "a column the brain has no title for")
            out[col or "no column"] += 1
        return out

    def tracking(self) -> dict:
        st = self.stage_of("dealsource", self.o["deal_stage"])
        return dealsource.tracking_lines(st) if st is not None else {}

    def researched_tags(self) -> list[str]:
        st = self.stage_of("researchstats", self.o["research_stage"])
        tags = (st.get("researched_tags") if st is not None else None) or []
        if not tags:
            ds = self.stage_of("dealsource", self.o["deal_stage"])
            tags = (ds.get("researched_tags") if ds is not None else None) or ["Deep Prospected"]
        return [str(t) for t in tags]

    # ================================================================== tab 5: Deals
    def tab_deals(self) -> dict:
        cat = self.cat
        risk = cat.share("goal.at_risk_share")
        wk = self.week(self.today - timedelta(days=self.today.weekday()))
        mo = self.month(self.today.replace(day=1))
        d90 = self.since_days(int(cat.value("goal.deal_window_days")))
        wins = (wk, mo, d90)
        walks = [self.appts(w)["total"] for w in wins]
        signed = [self.changes("contract_statuses", w) for w in wins]
        closed = [self.changes("closed_statuses", w) for w in wins]
        offers = [self.offers(w) for w in wins]
        o2c = [(_pct(s, o) if isinstance(o, int) else GAP) for s, o in zip(signed, offers)]
        w2c = _pct(signed[2], walks[2])
        cc = _pct(closed[2], signed[2])
        columns = ["Measure", wk.label, mo.label, d90.label, "Benchmark", "Mark"]
        rows = [
            ["Appointments set (walks booked)"] + walks + [NO_FIGURE, ""],
            ["Offers made"] + offers + [bench(cat, "role.closer_offers_per_day", "per closer"), ""],
            ["Offers considered, accepted, rejected", GAP, GAP, GAP, "", ""],
            ["Contracts signed"] + signed + ["", ""],
            ["Offer to contract (%)"] + o2c + ["", ""],
            ["Walk to contract (%)", "", "", w2c, cat.cite("funnel.walk_to_contract"),
             kpi.mark(w2c, cat.low("funnel.walk_to_contract"), risk) if isinstance(w2c, (int, float)) else ""],
            ["Deals closed"] + closed + ["", ""],
            ["Contracts that close (%)", "", "", cc, cat.cite("funnel.contracts_that_close"),
             kpi.mark(cc, cat.value("funnel.contracts_that_close"), risk) if isinstance(cc, (int, float)) else ""],
            ["Fees collected ($)"] + [self.revenue(w) for w in wins] + ["", ""],
        ]
        rows += [[], ["Contract ledger for the last %d days" % d90_days(cat)],
                 ["Record", "Contract signed", "Deal type", "Contract price", "Projected fee", "Assigned to a buyer",
                  "Closed", "Fee", "Days lead to contract", "Days contract to close", "Generated by", "Carried by",
                  "Closed by"]]
        rows += self.contract_ledger(d90)
        rows += [[], ["Dispositions"], ["Measure", "Value"],
                 ["Buyers added this week", GAP], ["Blasts sent", GAP], ["Replies", GAP], ["Offers from buyers", GAP],
                 ["Days from contract to assignment", GAP], ["Contracts that could not be assigned", GAP]]
        note = ("Contracts signed and deals closed stay on separate rows: a signed contract is never counted as a deal "
                "(team/kpi.yaml). Walks are the appointments set, since a walk is not logged apart from its "
                "appointment. %s %s Deal type, price and fees come from the Goals tab's deals: paste the record's "
                "DataSift link in the Property cell and the ledger row picks them up. The three names are the deal "
                "sources' reading of the call log, to confirm before paying on it. Dispositions are not tracked yet: "
                "the buyer registry and the blast tables live on the texting app and the workstation, not on the "
                "brain." % (self.offers_note(), self.revenue_note()))
        return tab("Deals", columns, rows, note + self.live_note())

    def contract_ledger(self, w: Win) -> list[list]:
        recorded = {d["record_uuid"]: d for d in dealsource.deals()}
        # A deal typed on the Goals tab with its DataSift link fills the ledger's money columns.
        typed = {}
        for t in self.typed_deals():
            m = RECORD_UUID.search(str(t.get("property") or ""))
            if m:
                typed[m.group(0).lower()] = t
        out = []
        for c in self.stage_records(w, "contract")[-int(self.o["ledger_rows"]):]:
            u, at = c["record"], c["when"]
            closed_at = self._status_after(u, at, self.o["closed_statuses"])
            moments = dealsource.lead_moments(u, until=at)
            d = recorded.get(u) or {}
            t = typed.get(str(u).lower()) or {}
            fee = (d.get("detail") or {}).get("fee") or t.get("fee")
            owner = self.owner_of(u)
            gen = d.get("generated_by") or (dealsource.origin(self.cat, owner, moments[-1])["caller"] if owner and moments else "")

            def money(v):
                return round(float(v)) if isinstance(v, (int, float)) else GAP
            out.append([self.link(u), _nice(at.astimezone(self.zone).date()), t.get("type") or GAP, money(t.get("price")),
                        money(t.get("expected")), GAP,
                        _nice(closed_at.astimezone(self.zone).date()) if closed_at else "",
                        round(float(fee)) if fee else (GAP if not (self.fee_field() or t) else ""),
                        (at - moments[-1]).days if moments else "before the log copy",
                        (closed_at - at).days if closed_at else "", _first_name(gen),
                        _first_name(d.get("carried_by") or ""), _first_name(d.get("closed_by") or "")])
        if not out:
            out.append(["no contract signed in the window"])
        return out

    # ================================================================== tab 6: First to Market
    def tab_ftm(self) -> dict:
        cat = self.cat
        cards, ever, live = self.board()
        cols = Counter(p.get("column") for p in cards.values())
        by_lane: dict = defaultdict(Counter)
        for p in cards.values():
            by_lane[p.get("column")][p.get("lane") or "ftm"] += 1
        lane_cols = [ln for ln in ftmpipeline.BOARD_LANES]
        columns = ["Column", "Cards"] + [lane_label(ln) for ln in lane_cols]
        rows = [[col, cols.get(col, 0)] + [by_lane[col].get(ln, 0) for ln in lane_cols] for col in BOARD_COLUMNS]
        rows.append(["On the board", len(cards)] + [sum(by_lane[c].get(ln, 0) for c in BOARD_COLUMNS) for ln in lane_cols])
        due = sum(1 for p in cards.values() if ftmpipeline.column_no(p.get("column") or "") in range(1, ftmpipeline.DAYS + 1)
                  and str(p.get("due") or "9999") <= self.today.isoformat())
        self.facts["cards_due"] = due
        rows.append(["Due a call today (Day 1 to Day 5)", due])
        # new records per feed
        rows += [[], ["New records per county feed"], ["Feed", "This week", "This month", "On the list now"]]
        for f in self.feeds():
            if f.get("missing"):
                rows.append([f["feed"], "no list with this title", "", ""])
            else:
                rows.append([f["feed"], self.val(f["week"]), self.val(f["month"]), self.val(f["total"])])
        # owners reached and leads from the board
        wk = self.week(self.today - timedelta(days=self.today.weekday()))
        mo = self.month(self.today.replace(day=1))
        owners_all = {str((cards.get(u) or {}).get("owner") or self.owner_of(u) or "").strip().lower() for u in ever}
        owners_all.discard("")
        ever_low = {str(u).strip().lower() for u in ever}
        rows += [[], ["From the board (every record the board ever held)"], ["Measure", wk.label, mo.label]]
        for label, fn in (("Owners worked (dialed or texted)", lambda w: len(self.worked_set(w) & owners_all)),
                          ("Owners reached", lambda w: len(self.reach_set(w) & owners_all)),
                          ("New leads", lambda w: sum(1 for ld in self.leads(w) if ld["record"] in ever_low))):
            rows.append([label, fn(wk), fn(mo)])
        st = self.stage_of(ftmpipeline.KIND, self.o["board_stage"])
        last = {}
        if st is not None:
            try:
                last = json.loads(store.kv_get("autopilot:last:%s" % st.id) or "{}")
            except ValueError:
                last = {}
        note = ("%s Cards by column come from the board stage's own memory%s. New records are records created this "
                "week or month that carry the feed's list (clean records); a record that already existed and gained "
                "the list is not counted, so this is a floor on what landed. Owners reached are reached by the contact "
                "rate's rule (a conversation, or a number marked correct)." % (
                    ("The board's last run: %s" % last["head"]) if last.get("head") else
                    ("The board stage has not run on this box yet." if st is not None else
                     "No board stage (kind ftmpipeline) is in the playbook."),
                    "" if live else ": the board is not live yet, so these are where its plan runs placed the cards"))
        return tab("First to Market", columns, rows, note + self.live_note())

    # ================================================================== tab 7: Direct Mail
    def mail_stages(self) -> list[tuple]:
        """(stage id, stage, lane, campaign) for every mail stage the tab reads: the monthly lanes (mail), the
        heir and obituary letters, and the campaigns timed from a date (mailcadence), named by their clock:
        Auction counts back from the sale, Probate forward from the day the estate opened."""
        wanted = [s for s in self.o["mail_stages"] if s]
        out = []
        for sid, st in (getattr(self.pol, "stages", None) or {}).items():
            if st.kind not in MAIL_STAGE_KINDS or (wanted and sid not in wanted) or (not wanted and st.mode == "off"):
                continue
            lane = str(st.get("lane") or {"heirlane": "heirs", "obituary": "obituary"}.get(st.kind, ""))
            if st.kind == "mailcadence":
                anchor = str(st.get("anchor") or "").strip().lower()
                name = CADENCE_NAMES.get(anchor) or sid.replace("_", " ").capitalize()
            else:
                name = lane_label(lane) if lane else sid.replace("_", " ")
            out.append((sid, st, lane, name))
        return out

    def tab_mail(self) -> dict:
        cat = self.cat
        mo = self.month(self.today.replace(day=1))
        columns = ["Campaign", "Step", "Piece", "Preset", "Price a piece ($)", "Staged now", "Released", "Mailed this month",
                   "Mailed to date", "Cost this month ($)", "Cost to date ($)"]
        rows = []
        lanes_seen = []
        month_pieces = 0
        said = []
        for sid, st, lane, name in self.mail_stages():
            if lane and lane not in lanes_seen:
                lanes_seen.append(lane)
            steps = self.mail_steps(sid, st, mo)
            for s in steps:
                cells = [s["staged"], s["released"], s["month"], s["ever"], round(s["usd_month"], 2), round(s["usd_ever"], 2)]
                if s.get("blocked") and not any(cells):
                    cells = [NONE_YET] * len(cells)          # a piece that cannot be ordered is never a row of zeros
                rows.append([name, s["step"], s["piece"], s["preset"], s["price"]] + cells)
                month_pieces += s["month"]
            if not steps:
                rows.append([name, "no piece staged or mailed yet", "", "", "", 0, 0, 0, 0, 0, 0])
            if st.kind == "mailcadence":
                said += self.cadence_notes(sid, st, name)
        if not rows:
            rows.append(["no mail stage in the playbook"])
        other = Counter(ks.tool_name(a.tool) for a in self.mail_acts()[0]
                        if not str(a.raw_ref or "").startswith("ap_events:") and a.at >= mo.start)
        for tool, n in sorted(other.items()):
            rows.append(["%s (another source)" % tool, "every step", "", "", "", "", "", n, "", "", ""])
            month_pieces += n
        # what DataSift's own counters say
        rows += [[], ["Records mailed by DataSift's own counters"],
                 ["Lane", "Records in the lane", "Mailed at least once", "Mailed this month", "Never mailed"]]
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        for lane in lanes_seen:
            scope = self.lane_scope(lane)
            if scope is None:
                rows.append([lane_label(lane), NOT_READ, NOT_READ, NOT_READ, NOT_READ])
                continue
            if not scope:
                rows.append([lane_label(lane), "no tag or list marks this lane", "", "", ""])
                continue
            total = self.count(scope, lane)
            ever = self.count(dict(scope, directmail_attempts=[1, 1000]), lane)
            month = self.count(dict(scope, last_direct_mailed=[mo.first.isoformat(), tomorrow]), lane)
            never = (total - ever) if isinstance(total, int) and isinstance(ever, int) else None
            rows.append([lane_label(lane), self.val(total), self.val(ever), self.val(month), self.val(never)])
        # response
        tracking = self.tracking()
        rows += [[], ["Response"], ["Measure", "Value", "Benchmark"]]
        rows.append(["Pieces mailed this month (every lane)", month_pieces, ""])
        if not tracking:
            rows.append(["Leads from mail this month", GAP, ""])
            rows.append(["Leads per 1,000 pieces", GAP, ""])
        else:
            n = self.mail_leads(mo, tracking)
            rows.append(["Leads from mail this month", n, ""])
            rows.append(["Leads per 1,000 pieces", _ratio(1000 * n, month_pieces) if n else NONE_YET, ""])
        deals = [d for d in dealsource.deals() if d.get("channel") == "mail"]
        rows.append(["Deals from mail to date (the deal sources' channel)", len(deals),
                     bench(cat, "source.exhausted_callbacks_per_deal", "on dialed-out records")])
        note = ("Each campaign's steps are the stage's own pieces list (a date-timed campaign's by each piece's id), so a "
                "new campaign order shows as its own steps. Staged and released count the autopilot's mail batches; "
                "mailed and cost count the pieces the mail sources reported (the ledger logs each when a batch goes "
                "out; cost is what the vendor billed, the price a piece is the vendor's table). The records counts are "
                "DataSift's own mail counter and last mailed date (clean records): a letter sent through a CSV vendor "
                "does not move them. Leads from mail need a call back on a mail tracking line%s.%s" % (
                    "" if tracking else ": no tracking line is set (mail_tracking_lines on the deal-source stage), so "
                    "they are not tracked yet", (" " + " ".join(said)) if said else ""))
        return tab("Direct Mail", columns, rows, note + self.live_note())

    def lane_scope(self, lane: str) -> dict | None:
        """The records-search filter that holds a lane's records: its list when the lane has one, else its
        tag (lanes markers). None when the id was not read; {} when nothing marks the lane."""
        m = (getattr(getattr(self.pol, "lanes", None), "markers", None) or lanes.DEFAULT_MARKERS).get(lane) or {}
        if m.get("lists"):
            lid = self.list_id(m["lists"][0])
            return None if lid is None else ({"any_lists": [lid]} if lid else {})
        if m.get("tags"):
            tid = self.tag_id(m["tags"][0])
            return None if tid is None else ({"any_tags": [tid]} if tid else {})
        return {}

    def mail_acts(self) -> tuple[list, dict, dict]:
        """(every mail piece a source reported, to date; ledger row id -> (stage, detail) for the pieces the
        ledger logged; the ledger row -> what it cost). A piece is counted through the sources; the ledger row
        it points at (raw_ref) says which stage and step it was, and the spend the ledger reports for the same
        row is its cost."""
        def read():
            w = Win("to date", EVER, self.now, EVER.date(), self.today + timedelta(days=1), True)
            acts = self.acts(w, "mail")
            cost = {a.raw_ref: float(a.amount or 0) for a in self.acts(w, "spend")
                    if str(a.raw_ref or "").startswith("ap_events:")}
            rows: dict = {}
            if any(str(a.raw_ref or "").startswith("ap_events:") for a in acts):
                with store._LOCK, store._db() as c:
                    for r in c.execute("SELECT id, stage, detail FROM ap_events WHERE action='mail'").fetchall():
                        try:
                            d = json.loads(r["detail"] or "{}")
                        except ValueError:
                            d = {}
                        rows["ap_events:%s" % r["id"]] = (str(r["stage"] or ""), d if isinstance(d, dict) else {})
            return acts, rows, cost
        return self._cached("mail_acts", read)

    def mail_steps(self, sid: str, st, mo: Win) -> list[dict]:
        """One row per step of a mail stage: its pieces list for a monthly mail stage, each piece's id for a
        date-timed campaign (mailcadence: A1 to A6, PB1 to PB9, EVG_T2, EVG_T4), or what the letters themselves
        say (series and letter number) for the heir and obituary lanes."""
        steps: dict = {}
        order: list = []
        by_no: dict = {}                 # a date-timed campaign's step number -> its piece id
        if st.kind == "mail":
            vendor = mail.vendor_for(st)
            for n, spec in enumerate([p for p in (st.get("pieces") or []) if isinstance(p, dict)], 1):
                piece = str(spec.get("piece") or "")
                steps[n] = {"step": n, "piece": mail.PIECE_LABEL.get(piece, piece), "preset": str(spec.get("preset") or ""),
                            "price": round(vendor.price(piece), 2)}
                order.append(n)
        elif st.kind == "mailcadence":
            from . import mailcadence as mc
            vendor = mail.vendor_for(st)
            table = mc.price_table(mc.options(st)) if getattr(vendor, "batch", False) else None
            for d in mc.designs_of(st):
                by_no[d.no] = d.id
                blocked = table is not None and not orderable(d, table)
                steps[d.id] = {"step": d.id, "piece": mail.PIECE_LABEL.get(d.kind, d.kind.replace("_", " ")), "preset": "",
                               "price": CANNOT_ORDER if blocked else round(vendor.price(d.kind), 2), "blocked": blocked}
                order.append(d.id)

        def key_of(d: dict):
            if st.kind == "mail":
                return _int(d.get("step"))
            if st.kind == "mailcadence":
                return by_no.get(_int(d.get("step"))) or str((d.get("merge") or {}).get("design") or "step %d" % _int(d.get("step")))
            # A staged heir letter carries no series; its ledger row says "heir". One key for both.
            series = d.get("series") or ("heir" if st.kind == "heirlane" else "letter")
            return "%s %d" % (series, _int(d.get("letter") or d.get("step")))

        def row(k, d: dict):
            if k not in steps:
                piece = str(d.get("piece") or "")
                steps[k] = {"step": k if st.kind in ("mail", "mailcadence") else str(k), "piece": mail.PIECE_LABEL.get(piece, piece),
                            "preset": "no longer in the playbook" if st.kind in ("mail", "mailcadence") else "",
                            "price": d.get("price") or ""}
                order.append(k)
            return steps[k]

        counts = defaultdict(Counter)
        for b in ledger.batches(stage=sid):
            if b.get("kind") not in MAIL_BATCH_KINDS or b.get("status") not in ("staged", "sent"):
                continue
            full = ledger.batch(int(b["id"])) or {}
            for it in full.get("items") or []:
                k = key_of(it)
                row(k, it)
                counts[k]["staged" if b["status"] == "staged" else "released"] += 1
        acts, rows, cost = self.mail_acts()
        for a in acts:
            stage, d = rows.get(a.raw_ref, ("", {}))
            if stage != sid:
                continue
            k = key_of(d)
            row(k, d)
            usd = int(round(cost.get(a.raw_ref, 0.0) * 10000))
            counts[k]["ever"] += 1
            counts[k]["usd_ever_c"] += usd
            if a.at >= mo.start:
                counts[k]["month"] += 1
                counts[k]["usd_month_c"] += usd
        out = []
        for k in order:
            s = dict(steps[k])
            c = counts[k]
            s.update(staged=c["staged"], released=c["released"], ever=c["ever"], month=c["month"],
                     usd_ever=c["usd_ever_c"] / 10000.0, usd_month=c["usd_month_c"] / 10000.0)
            out.append(s)
        return out

    def cadence_notes(self, sid: str, st, name: str) -> list[str]:
        """What a date-timed campaign says beyond its counts, as notes and never as zeros: the pieces SiftMail
        cannot order yet (mailcadence's own price-row test) and the records whose piece waits on the SiftMail
        floor (its wait memory)."""
        from . import mailcadence as mc
        vendor = mail.vendor_for(st)
        if not getattr(vendor, "batch", False):
            return []
        o = mc.options(st)
        table = mc.price_table(o)
        designs = mc.designs_of(st)
        by_id = {d.id: d for d in designs}
        out = []
        for d in designs:
            if orderable(d, table):
                continue
            fb = by_id.get(d.fallback)
            out.append("%s %s (%s) %s: SiftMail has no price for mail type %s in this account, so %s." % (
                name, d.id, mail.PIECE_LABEL.get(d.kind, d.kind.replace("_", " ")), CANNOT_ORDER, d.mail_type,
                ("its fallback %s goes in its place" % fb.id) if fb is not None and orderable(fb, table)
                else "its slot goes to the next piece"))
        waits = dict(self.floor_waits().get(sid) or {})
        for pid, (n, since) in (self.floor_waits().get("") or {}).items():
            if pid in by_id:              # a wait whose record no campaign claims any more: filed by its piece id
                have = waits.get(pid, [0, None])
                waits[pid] = [have[0] + n, min([x for x in (have[1], since) if x is not None], default=None)]
        if waits:
            floor = max(1, _int(o.get("min_batch")) or 1)
            parts = ["%s %s" % (pid, _plural(n, "record")) for pid, (n, _since) in sorted(waits.items())]
            oldest = min((s for _n, s in waits.values() if s is not None), default=None)
            out.append("%s pieces waiting on SiftMail's floor of %d records a campaign: %s%s." % (
                name, floor, ", ".join(parts), (", the longest since %s" % _nice(oldest)) if oldest else ""))
        return out

    def floor_waits(self) -> dict:
        return self._cached("floor_waits", floor_waits)

    def mail_leads(self, mo: Win, tracking: dict) -> int:
        """New leads this month whose conversation came in on a mail tracking line (dealsource.origin)."""
        n = 0
        for ld in self.leads(mo):
            owner = self.owner_of(ld["record"])
            if owner and dealsource.origin(self.cat, owner, ld["when"], tracking).get("channel") == "mail":
                n += 1
        return n

    # ================================================================== tab 8: Channels
    def tab_channels(self) -> dict:
        cat = self.cat
        this = self.month(self.today.replace(day=1))
        last = self.month((self.today.replace(day=1) - timedelta(days=1)).replace(day=1))
        wins = (this, last)
        columns = ["Channel", "Measure", this.label, last.label, "Benchmark"]
        c = self.calls
        rows = [
            ["Calls", "Dials"] + [c(w)["dials"] for w in wins] + [cat.cite("caller.dials_per_day")],
            ["Calls", "Conversations"] + [c(w)["conversations"] for w in wins] + [cat.cite("caller.conversations_per_day")],
            ["Calls", "Numbers confirmed on a call"] + [self.correct(w)["by_call"] for w in wins] + [""],
            ["Calls", "New leads (every channel)"] + [len(self.leads(w)) for w in wins] + [cat.cite("caller.opportunities_per_day")],
            ["Calls", "Appointments"] + [self.appts(w)["total"] for w in wins] + [NO_FIGURE],
            ["Calls", "Contracts signed"] + [self.changes("contract_statuses", w) for w in wins] + [""],
        ]
        tools = self.texting()
        named = {ks.tool_key(c.get("tool")) for c in tools if c.get("tool")}
        if "smartercontact" not in named:
            rows.append(["Texting (Smarter Contact)", "Sent, delivered, replies, hand raises, opt-outs", GAP, GAP,
                         cat.cite("channel.smarter_contact_leads_per_day")])
        for t in sorted(named):
            label = "Texting (%s)" % ks.tool_name(t)
            rows += [[label, "Texts sent"] + [self.texts_sent(w, t) for w in wins]
                     + [cat.cite("channel.smarter_contact_leads_per_day") if t == "smartercontact" else ""],
                     [label, "Replies"] + [self.replies(w, t) for w in wins] + [""]]
        rows += [["Texting (Smarter Contact)", "Numbers confirmed by a text reply or a person"] + [self.correct(w)["other"] for w in wins] + [""],
                 ["Texting (the autopilot)", "Texts released by the autopilot"] + [
                     (lambda v: "could not be read" if v is None else v)(self.texts_released(w)) for w in wins]
                 + [cat.cite("channel.leads_per_100_touches")],
                 ["Texting (the texting app)", "Sent, replies, hot leads, opt-outs", GAP, GAP, ""]]
        if tools:
            rows.append(["Texting (every source)", "Texts sent"] + [self.texts_sent(w) for w in wins] + [""])
            rows.append(["Texting (every source)", "Replies"] + [self.replies(w) for w in wins] + [""])
        agency = [self.agency_sent(w) for w in wins]
        back = [self.agency_back(w) for w in wins]
        self.facts["agency_leads_back"] = back[0] if isinstance(back[0], int) else None
        fee = self.agency_fee()
        rows += [["Agency", "Owners sent"] + agency + [""],
                 ["Agency", "Calls in its file"] + [self.agency_calls(w) for w in wins] + [""],
                 ["Agency", "Leads back from its file"] + back + [cat.cite("channel.calling_agency_leads_per_day")],
                 ["Agency", "Monthly fee ($)", fee, fee, ""]]
        rows += self.tally_rows(wins)
        mail_m, mail_usd = [], []
        for w in wins:
            got = [a for a in self.acts(w, "mail")]
            mail_m.append(len(got))
            mail_usd.append(round(sum(float(a.amount or 0) for a in self.acts(w, "spend")
                                      if str(a.raw_ref or "").startswith("ap_events:") and a.outcome == "mail"), 2))
        rows += [["Mail", "Pieces mailed (the Direct Mail tab has each step)"] + mail_m + [""],
                 ["Mail", "Cost ($)"] + mail_usd + [""]]
        tags = self.researched_tags()
        res = [len(dealsource_researched(tags, w)) for w in wins]
        checks = [r for r in research_checks()]
        tbl = research_table(checks)
        rows += [["Deep prospecting", "Records researched"] + res + [""],
                 ["Deep prospecting", "Reached within a month of research (every check so far)", "%s of %s (%s%%)" % (
                     tbl["reached"], tbl["checked"], _pct(tbl["reached"], tbl["checked"])) if tbl["checked"] else NONE_YET,
                  "", cat.cite("research.reached")],
                 ["Deep prospecting", "Research cost ($)"] + [round(self.spend(w)["research"], 2) for w in wins] + [""]]
        ni = [self.deals_by_source(w, "not interested follow-up") for w in wins]
        rh = [self.deals_by_source(w, "an old lead worked again") for w in wins]
        rows += [["Not Interested and rehash", "Deals from a Not Interested owner called again"] + ni
                 + [cat.cite("source.not_interested_share_of_deals")],
                 ["Not Interested and rehash", "Deals from an old lead worked again"] + rh + [cat.cite("source.rehash_share_of_deals")],
                 ["Not Interested and rehash", "Leads", GAP, GAP, ""]]
        feeds = self.feeds()
        month_new = [f.get("month") for f in feeds if not f.get("missing")]
        rows.append(["County records", "New records this month (the First to Market tab has each feed)",
                     sum(v for v in month_new if isinstance(v, int)) if month_new and all(isinstance(v, int) for v in month_new)
                     else NOT_READ, "", ""])
        note = ("This month so far beside last month. %s A reply that confirms a number is counted under numbers "
                "confirmed by a text or a person. The agency's rows are its own files, read as their own source: calls "
                "in its file are the numbers it dialed (not tracked yet until a file for the month comes back), and a "
                "figure its report gives is shown under the report's own word, never as ours. The fee is the agency "
                "scorecard's monthly_fee. Leads by channel for Not Interested and rehash need the lead ledger to carry "
                "the source, which it does from this sheet on; deals by source are the deal sources' rows." % (
                    self.texts_note()))
        if not self.agency_ids():
            note += " The agency's files are switched off as a KPI source, so its calls are not tracked."
        return tab("Channels", columns, rows, note + self.live_note())

    def agency_calls(self, w: Win):
        """The numbers the agency dialed in the window, from its own files (the built-in agency source), or
        "not tracked yet" when no file of its holds the window."""
        n = sum(1 for a in self.acts(w, "agency") if a.kind == "call" and a.direction == "out")
        return n if n else GAP

    def tally_rows(self, wins) -> list[list]:
        """A total a source gives instead of events (the agency's report, a tool's daily totals): one row per
        source and figure, under the source's own word, never added to the events and never called ours.
        A figure the events already count for the same tool and period says so."""
        seen: dict = {}
        labels = {c["id"]: ks.label(c) for c in self.connections()}
        agency = set(self.agency_ids())
        for i, w in enumerate(wins):
            got = {}
            for group in ("calls", "phone", "stage", "mail", "spend", "agency"):
                for t in self.collected(w, group)["tallies"]:
                    got[(t.source, t.metric, t.period, t.person_ref, t.definition)] = t
            for t in got.values():
                row = seen.setdefault((t.source, t.metric, t.definition or t.metric, t.person_ref),
                                      {"vals": [None] * len(wins), "covered": False})
                row["vals"][i] = (row["vals"][i] or 0.0) + float(t.value)
                row["covered"] = row["covered"] or bool(t.covered)
        out = []
        for (src, _metric, word, person), row in sorted(seen.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][3])):
            vals = [NONE_YET if v is None else (int(v) if float(v).is_integer() else round(v, 1)) for v in row["vals"]]
            who = (" (%s)" % _first_name(person_name(person))) if person_name(person) else ""
            lab = "Agency" if src in agency else labels.get(src, src)
            out.append([lab[:1].upper() + lab[1:], "%s%s, as its report says" % (word, who)] + vals
                       + ["the events count this too" if row["covered"] else "its own word, not ours"])
        return out

    def agency_sent(self, w: Win):
        from . import agencyscore
        s = agencyscore.sent_in(w.start, w.end, self.tz)
        return s["owners"] or s["houses"]

    def agency_back(self, w: Win):
        """Leads back from the agency's returned file for the month, or GAP when none came back."""
        from . import agencyscore
        from ..workflows import agency_results as ar
        label = w.first.strftime("%Y-%m")
        mine = [r for r in ar.load_returns() if r.get("month") == label and r.get("kind") == "records"]
        if not mine:
            return GAP
        return int(agencyscore.agency_side(mine)["counts"].get("lead") or 0)

    def agency_fee(self):
        """The calling agency's fee a month: typed on the Goals tab (Calling agency), else the agency
        scorecard stage's monthly_fee."""
        typed = self.cat.monthly_costs().get("agency") if self.cat is not None else None
        if typed is not None:
            return round(float(typed))
        st = self.stage_of("agencyscore", self.o["agency_stage"])
        fee = st.get("monthly_fee") if st is not None else None
        return round(float(fee)) if isinstance(fee, (int, float)) and not isinstance(fee, bool) and fee > 0 else GAP

    def deals_by_source(self, w: Win, source: str) -> int:
        return sum(1 for d in dealsource.deals(since_day=w.first.isoformat(), until_day=w.nxt.isoformat())
                   if d.get("source") == source)

    # ================================================================== tab 9: Lists
    def tab_lists(self) -> dict:
        cat = self.cat
        mo = self.month(self.today.replace(day=1))
        reach = kpi.log_reach(contactrate.TOUCHES)
        since = reach or (self.now - timedelta(days=activity.FLOOR_DAYS))
        span = Win("since the log copy starts", since, self.now, since.astimezone(self.zone).date(),
                   self.today + timedelta(days=1), True)
        worked = self.worked_set(span)
        reached_all = self.reach_set(span)
        reached_month = self.reach_set(mo)
        per: dict = defaultdict(lambda: {"owners": set(), "records": 0})
        for owner, (lane, recs) in self.lanes_map().items():
            per[lane]["owners"].add(owner)
            per[lane]["records"] += len(recs)
        deals_by_lane = Counter(d.get("lane") or "" for d in dealsource.deals())
        columns = ["Lane", "Owners", "Records", "Owners worked (since the log copy starts)", "Owners reached (since then)",
                   "Reached this month", "Share reached (%)", "Leads", "Deals closed", "Doors per deal", "Forecast"]
        rows = []
        for lane in [ln for ln in lanes.ORDER if ln in per] + [ln for ln in per if ln not in lanes.ORDER]:
            owners = per[lane]["owners"]
            w_owners = owners & worked
            r_all = w_owners & reached_all
            r_month = owners & reached_month
            rows.append([lane_label(lane), len(owners), per[lane]["records"], len(w_owners), len(r_all), len(r_month),
                         _pct(len(r_all), len(w_owners)), GAP, deals_by_lane.get(dealsource.LANE_WORDS.get(lane, lane), 0),
                         GAP, GAP])
        if not rows:
            rows.append(["the owner-lane map is empty (the nightly owner_lanes stage has not run)"])
        rows += [[], ["County lists"], ["List", "Records on the list", "New this month", "Turnover this month (%)",
                                        "Doors per deal"]]
        for f in self.feeds():
            if f.get("missing"):
                rows.append([f["feed"], "no list with this title", "", "", ""])
                continue
            t, m = f.get("total"), f.get("month")
            rows.append([f["feed"], self.val(t), self.val(m),
                         _pct(m, t) if isinstance(t, int) and isinstance(m, int) else NOT_READ, GAP])
        m = contactrate.measure(self.now, self.tz, cat, weeks=int(self.o["weeks"]))
        rows += [[], ["Contact rate by week (owners first worked that week and reached since)"],
                 ["Week", "Owners", "Reached", "Share (%)"]]
        for r in m["weeks"]:
            rows.append(["Week %d (%s)%s" % (r["week"], r["start"], ", partly before the log copy" if r["clipped"] else ""),
                         r["owners"], r["reached"], _pct(r["reached"], r["owners"])])
        rows += [[], ["Contact rate by lane over those weeks"], ["Lane", "Owners", "Reached", "Share (%)"]]
        for r in m["lanes"]:
            if r["owners"]:
                rows.append([contactrate.LANE_WORDS.get(r["lane"], "not on a lane today"), r["owners"], r["reached"],
                             _pct(r["reached"], r["owners"])])
        note = ("Lanes are the nightly owner-lane map: where each owner sits today, so a lead's owner sits in the lead "
                "lane and leads per lane are not tracked yet (the lead ledger carries them from now on). Worked and "
                "reached reach back to %s, where the log copy starts. Doors per deal and the forecast wait on the "
                "doors-per-deal figure for each list (decision F13). Turnover is new records this month over the list's "
                "size; the catalog says %s." % (
                    since.astimezone(self.zone).date().isoformat(), cat.cite("cycle.list_turnover_per_month")))
        return tab("Lists", columns, rows, note + self.live_note())

    # ================================================================== tab 10: Goals
    def tab_goals(self) -> dict:
        cat = self.cat
        need = goals.needs(cat)
        risk = cat.share("goal.at_risk_share")

        def status(key):
            f = cat.fig(key)
            if kpi.TYPED_SOURCE in f.source:
                return "yours, typed on the Goals tab"
            return f.tag() or "confirmed"

        columns = ["Input", "Value", "Status", "Decision", "Source"]
        conv_per_dial = cat.low("caller.conversations_per_day") / cat.value("caller.dials_per_day")
        rows = []
        for key, value in (("goal.revenue_per_year", cat.text("goal.revenue_per_year")),
                           ("goal.average_deal", cat.text("goal.average_deal")),
                           ("funnel.contracts_that_close", cat.text("funnel.contracts_that_close")),
                           ("funnel.leads_per_deal", "%s (the cautious end, %s)" % (cat.text("funnel.leads_per_deal"),
                                                                                  kpi.num(cat.high("funnel.leads_per_deal")))),
                           ("funnel.dials_per_lead", "%s (the cautious end, %s)" % (cat.text("funnel.dials_per_lead"),
                                                                                  kpi.num(cat.high("funnel.dials_per_lead")))),
                           ("caller.conversations_per_day", "%s (%s on %s)" % (
                               kpi.num(round(conv_per_dial, 3)), cat.text("caller.conversations_per_day"),
                               cat.text("caller.dials_per_day"))),
                           ("goal.at_risk_share", cat.text("goal.at_risk_share"))):
            f = cat.fig(key)
            label = "Conversations per dial" if key == "caller.conversations_per_day" else f.label.capitalize()
            rows.append([label, value, status(key), f.decision, ", ".join(f.source)])
        rows += [[], ["Needed a month worked back"], ["Measure", "A month"]]
        for k in ("deals", "contracts", "leads", "dials", "conversations"):
            rows.append([k.capitalize(), _n1(need[k])])
        # pace: the goal sheet's windows, scaled to the days the log copy holds
        act, deal = int(cat.value("goal.activity_window_days")), int(cat.value("goal.deal_window_days"))
        calls_first = self._reach_day(activity.CALLS)
        state_first = self._reach_day("property.status.updated")
        aw, dw = self.since_days(act), self.since_days(deal)
        aw = Win(aw.label, aw.start, self.today_start, aw.first, self.today, False)
        dw = Win(dw.label, dw.start, self.today_start, dw.first, self.today, False)
        span_a = goals._span(act, self.today, calls_first)
        span_l = goals._span(act, self.today, state_first)
        span_d = goals._span(deal, self.today, state_first)
        rows += [[], ["Pace in the goal sheet's windows (up to yesterday)"], ["Measure", "Window", "Actual", "Needed so far", "Mark"]]
        for label, got, month_need, span, full in (
                ("Dials", self.calls(aw)["dials"], need["dials"], span_a, act),
                ("Conversations", self.calls(aw)["conversations"], need["conversations"], span_a, act),
                ("New leads", len(self.leads(aw)), need["leads"], span_l, act),
                ("Contracts signed", self.changes("contract_statuses", dw), need["contracts"], span_d, deal),
                ("Deals closed", self.changes("closed_statuses", dw), need["deals"], span_d, deal)):
            needed = month_need * span / 30.0
            rows.append([label, ("last %d days" % full) if span >= full else ("%d of the %d days (the log copy is younger)" % (span, full)),
                         got, _n1(needed), kpi.mark(got, needed, risk) if span else "not judged"])
        # since the log copy starts (the course's year to date, as far as the copy reaches)
        start = self._reach_moment()
        yt = Win("since the log copy starts", start, self.now, start.astimezone(self.zone).date(), self.today + timedelta(days=1), True)
        dials, leads_n = self.calls(yt)["dials"], len(self.leads(yt))
        signed, closed = self.changes("contract_statuses", yt), self.changes("closed_statuses", yt)
        dpl, lpc, cc = _ratio(dials, leads_n), _ratio(leads_n, signed), _pct(closed, signed)
        rows += [[], ["Since the log copy starts (%s)" % yt.first.isoformat()], ["Ratio", "Value", "Benchmark", "Mark"],
                 ["Dials per lead", dpl, cat.cite("funnel.dials_per_lead"),
                  kpi.mark(dpl, cat.high("funnel.dials_per_lead"), risk, lower_is_better=True) if isinstance(dpl, (int, float)) else ""],
                 ["Leads per contract", lpc, cat.cite("funnel.leads_per_deal"),
                  kpi.mark(lpc, cat.high("funnel.leads_per_deal"), risk, lower_is_better=True) if isinstance(lpc, (int, float)) else ""],
                 ["Offers per contract", GAP, "", ""],
                 ["Contracts that close (%)", cc, cat.cite("funnel.contracts_that_close"),
                  kpi.mark(cc, cat.value("funnel.contracts_that_close"), risk) if isinstance(cc, (int, float)) else ""]]
        lines, _ = goals.owner_review(self.ctx, cat, self.today)
        rows += [[], ["Quarterly owner review"]] + [[ln] for ln in lines[1:]]
        read_at = _moment((getattr(cat, "typed", None) or {}).get("at"))
        note = ("Each input is the number typed on the Goals tab of the Google Sheet when one is typed there, and "
                "team/kpi.yaml's otherwise, so the sheet, the Slack update and the Monday goal sheet use the same numbers: "
                "change one on the Goals tab, not here.%s The pace counts from the connected sources (the log copy among "
                "them), so it can read above the Monday goal sheet on a day the daily capture missed. %s" % (
                    (" The Goals tab was last read %s at %s." % (_nice(read_at.astimezone(self.zone).date()),
                                                                 _clock(read_at.astimezone(self.zone))))
                    if read_at else "", footnote(cat, goals.USES + goals.REVIEW_USES)))
        return tab("Goals", columns, rows, note + self.live_note())

    def _reach_day(self, types) -> date | None:
        r = kpi.log_reach(types)
        return r.astimezone(self.zone).date() if r else None

    def _reach_moment(self) -> datetime:
        r = kpi.log_reach(activity.CALLS + ("property.status.updated",))
        return r or (self.now - timedelta(days=activity.FLOOR_DAYS))

    # ================================================================== tab 11: Costs
    def tab_costs(self) -> dict:
        cat = self.cat
        months = self.months()
        columns = ["Cost"] + [w.label for w in months] + ["Benchmark"]
        typed = {k: v for k, v in cat.monthly_costs().items() if k != "agency"}     # the agency has its own row
        fee = self.agency_fee()
        rows = [["Mail (the autopilot's ledger) ($)"] + [round(self.spend(w)["mail"], 2) for w in months] + [""],
                ["Skip trace and phone scoring (the ledger) ($)"] + [round(self.spend(w)["data"], 2) for w in months] + [""],
                ["Research (the brain's spend) ($)"] + [round(self.spend(w)["research"], 2) for w in months] + [""]]
        for name in ("dialer", "texting", "salaries", "subscriptions"):
            v = typed.get(name)
            rows.append(["%s ($ a month, typed)" % name.capitalize()] + [round(v) if v is not None else GAP] * len(months) + [""])
        for name in sorted(n for n in typed if n not in ("dialer", "texting", "salaries", "subscriptions")):
            rows.append(["%s ($ a month, typed)" % name.capitalize()] + [round(typed[name])] * len(months) + [""])
        rows.append(["Calling agency ($ a month)"] + [fee] * len(months) + [""])
        totals = []
        for w in months:
            t = sum(self.spend(w).values()) + sum(typed.values()) + (fee if isinstance(fee, (int, float)) else 0)
            totals.append(round(t, 2))
        rows.append(["Total known ($)"] + totals + [""])
        for label, fn, bench in (("Cost per lead ($)", lambda w: len(self.leads(w)), ""),
                                 ("Cost per contract ($)", lambda w: self.changes("contract_statuses", w), ""),
                                 ("Cost per closed deal ($)", lambda w: self.changes("closed_statuses", w), cat.cite("owner.cost_per_deal"))):
            vals = []
            for w, t in zip(months, totals):
                if not cat.monthly_costs():      # the fixed costs unknown: a cost per lead would read too low
                    vals.append(GAP)
                    continue
                n = fn(w)
                vals.append(round(t / n) if n else NONE_YET)
            rows.append([label] + vals + [bench])
        rev = [self.revenue(w) for w in months]
        margins = [(_pct(r - t, r, 0) if isinstance(r, (int, float)) and r else (GAP if r == GAP else NONE_YET))
                   for r, t in zip(rev, totals)]
        rows.append(["Net margin (%)"] + margins + [cat.cite("owner.net_margin")])
        runway = cat.input("runway_months")
        rows.append(["Months of runway (typed)"] + [runway if runway is not None else GAP] * len(months)
                    + [cat.cite("pacing.runway_months")])
        rows += [[], ["Spend by source and tool ($)"], ["Source and tool"] + [w.label for w in months]]
        rows += self.spend_by_source(months)
        missing = [n for n in ("dialer", "texting", "salaries", "subscriptions") if n not in typed]
        note = ("Mail, data and research are what the autopilot paid, read from the spend the sources report (the ledger "
                "and the brain's spend table, each dollar once). %s Cost per lead, contract and deal are not tracked until "
                "a monthly cost is typed on the Goals tab of the Google Sheet (or under inputs.monthly_costs in "
                "team/kpi.yaml); a typed cost counts in every month shown. The net margin waits on revenue: %s Spend by "
                "source and tool also shows what the totals above leave out (the team's own model turns), and any spend "
                "a connected tool reports." % (
                    ("Not typed yet: %s." % ", ".join(missing)) if missing else "Every named monthly cost is typed.",
                    self.revenue_note()))
        return tab("Costs", columns, rows, note + self.live_note())

    def spend_by_source(self, months) -> list[list]:
        """One row per source and tool: every dollar a connected source reports, month by month, or one row
        reading not tracked yet when no source reports spend."""
        if not any("spend" in self.kinds_of(c) for c in self.connections() if not self.is_agency(c)):
            return [["Spend (no spend source is connected)"] + [GAP] * len(months)]
        labels = {c["id"]: ks.label(c) for c in self.connections()}
        got: dict = defaultdict(lambda: [0.0] * len(months))
        for i, w in enumerate(months):
            for a in self.acts(w, "spend"):
                lab = labels.get(a.sources[0] if a.sources else a.source, a.source)
                got["%s, %s" % (ks.tool_name(a.tool), lab)][i] += float(a.amount or 0)
        if not got:
            return [["no spend reported yet"] + [0] * len(months)]
        return [[k[:1].upper() + k[1:]] + [round(v, 2) for v in vals] for k, vals in sorted(got.items())]

    # ================================================================== tab 12: How it is counted
    WORDS = (
        ("Dial", "One outbound call to one number by our callers, whichever dialer made it and however many sources "
                 "report it (the KPI sources join the reports into one act)."),
        ("Answered", "A call a source says was answered. A voicemail greeting answers too, so an answer is not a "
                     "conversation; an answered call no source gave a length for is never one."),
        ("Owners dialed", "Distinct owners with an outbound dial in the window."),
        ("Hours on the dialer", "A caller's first dial to their last that day, each dial's own moment; nobody types it."),
        ("Owners reached", "Owners with a conversation or a number marked correct in the window (the contact rate's rule)."),
        ("Owners reached by call", "Owners with a call conversation, or a number a dialer marked correct, in the window."),
        ("Owners reached by text", "Owners with a text conversation, a first reply, or a number the texting app marked "
                                   "correct on a reply, in the window."),
        ("Outreach texts", "Texts sent before any reply: what the autopilot released into the texting tool (its own "
                           "ledger), and the outreach a connected texting tool reports. A reply of ours is not outreach."),
        ("Initial text reply", "An owner's first text back in a thread, counted once, in the window it landed in."),
        ("Talk time a conversation", "The mean length of a call that counts as a conversation, as minutes:seconds."),
        ("Qualified lead", "A lead moved to one of the qualified statuses on the stage (decision F9), read from the log copy."),
        ("Pass-off", "An appointment set, until the acquisitions columns are named (decision F9)."),
        ("Appointment", "A record any source reports at the appointment stage (an appointment task in DataSift), or a "
                        "card moved into the Appointment Booked column."),
        ("Offer", "A record the sources report at the offer stage; not tracked yet while nothing is named an offer."),
        ("Contract", "A record the sources report at the contract stage (a contract status). Never counted as a deal."),
        ("Full attempt", "Every number on the record, a voicemail and a text in one day (fullattempt.py)."),
        ("Lead attempts", "Our calls and the texts sent to owners the nightly lane map puts in the lead lane."),
        ("Phone marks", "Numbers marked wrong, dead or do-not-call in the window, each number once per mark; the team's."),
        ("Speed to lead", "Minutes from New Lead to the first outbound call (leadclock.first_call); the middle of the waits."),
        ("New records in a feed", "Records created in the window that carry the feed's list (clean records)."),
        ("The agency's work", "Its own files, read as their own source: shown on the agency's rows, never among our "
                              "callers' numbers."),
        ("Not tracked yet", "A number nothing can read yet. It is never written as zero."),
    )

    def tab_counted(self) -> dict:
        cat = self.cat
        columns = ["Word", "What it means", "Source", "Status"]
        rows = []
        for name in ("conversation", "contact", "opportunity", "correct_number", "deal"):
            d = cat.definition(name)
            rows.append([name.replace("_", " ").capitalize(), " ".join(str(d.get("says") or "").split()),
                         ", ".join(str(s) for s in d.get("source") or []), str(d.get("status") or "")])
        for word, says in self.WORDS:
            rows.append([word, says, "this tracker", ""])
        rows += [[], ["Figures the tabs are judged against"], ["Figure", "Value", "Source", "Status"]]
        for key in USES:
            if key not in cat.figures:
                rows.append([key, "missing from team/kpi.yaml: the tab that needs it stops", "", ""])
                continue
            f = cat.fig(key)
            rows.append([key, f.text(), ", ".join(f.source), f.tag() or "confirmed"])
        rows += [[], ["Data notes"], ["Note", "Detail"]]
        for g in self.o["groups"]:
            t = activity.synced_through(g)
            r = kpi.log_reach(activity.GROUPS.get(g, ()))
            rows.append(["The %s log copy" % g, "starts %s, read through %s" % (
                r.astimezone(self.zone).date().isoformat() if r else "nowhere yet",
                t.astimezone(self.zone).strftime("%b %d %I:%M %p").replace(" 0", " ") if t else "never")])
        rows += [["Texts", self.texts_note()],
                 ["Offers", self.offers_note()],
                 ["Lead sources", "A caller when the log holds a conversation of ours before the lead; a call-in or a "
                  "text reply when that was the conversation; texts when the autopilot texted the owner, or a number "
                  "of theirs was marked correct outside a call, in the 30 days before; the autopilot when our own key "
                  "set the status; DataSift automation when its system user did; else no source found."],
                 ["Live counts", "Clean records (the app's first tab), at most %d a run." % int(self.o["max_counts"])],
                 ["Refresh", "%s. Every tab is rebuilt at each run except %s." % (
                     ", ".join(getattr(self.ctx.stage, "schedule", None) or []) or "the stage's schedule",
                     "; ".join("%s, at the %s run" % (k, " and ".join(v))
                               for k, v in sorted((self.o.get("tab_runs") or {}).items())) or "none")],
                 ["Deleted tabs", "A tab deleted by hand on the sheet stays deleted; the writer never adds it back."]]
        # the sources every number above was read from
        rows += [[], ["The KPI sources as this run read them"],
                 ["Source", "What it provides", "Last reported", "Status"]]
        for c in self.connections():
            r = self.source_rows.get(c["id"]) or {}
            kinds = [KIND_WORDS.get(k, k) for k in ks.KINDS if k in self.kinds_of(c)]
            last = r.get("last_at")
            status = "; ".join(r.get("problems") or []) or ("ok" if r.get("read") else "not read this run")
            if r.get("through") is not None and r.get("read"):
                status += ", complete through %s" % _stamp(r["through"], self.zone)
            what = ", ".join(kinds) or "nothing this tracker reads"
            if self.is_agency(c):
                what += " (the agency's rows only)"
            lab = ks.label(c)
            rows.append([lab[:1].upper() + lab[1:], what, _stamp(last, self.zone) if last else "nothing yet", status])
        pub = ks.public_health() or {}
        if pub:
            rows.append(["On the box", "%s switched on, %s of them webhooks; %s kept, %s not read yet" % (
                _plural(_int(pub.get("sources_on")), "source"), kpi.num(_int(pub.get("webhooks_on"))),
                _plural(_int(pub.get("deliveries_kept")), "delivery", "deliveries"),
                kpi.num(_int(pub.get("deliveries_waiting")))), "", ""])
        rows += [[], ["What the sources said this run"], ["Issue", "Touches"]]
        for text, kinds in self.issues.items():
            rows.append([text, ", ".join(KIND_WORDS.get(k, k) for k in ks.KINDS if k in kinds)])
        for text in self.source_notes:
            rows.append([text, "a note"])
        if not self.issues and not self.source_notes:
            rows.append(["none: every source read cleanly", ""])
        return tab("How it is counted", columns, rows, "Every definition is read from team/kpi.yaml and every act from "
                                                       "the connected KPI sources; the tracker adds no number of its own. "
                                                       "Connect a tool with python src/agents/cli.py kpisources connect; "
                                                       "see what each source said with kpisources status.")

    # ================================================================== the Slack lines
    def slack_lines(self, failed: list[str]) -> list[str]:
        f = self.facts
        cat = self.cat
        lines = ["KPI tracker refreshed %s at %s." % (self.local.strftime("%a, %b %d").replace(" 0", " "), _clock(self.local))]
        day = f.get("day") or {}
        if self.run == "morning":
            if day and (day.get("dials") or day.get("conversations") or day.get("leads") or day.get("correct")):
                lines.append("%s final: %s." % (_day_word(date.fromisoformat(day["first"])) if day.get("first") else day["label"],
                                                self._day_text(day)))
            lw = f.get("last_week") or {}
            if lw and (lw.get("dials") or lw.get("leads") or lw.get("appointments") or lw.get("contracts")):
                lines.append("Last week: %s, %s, %s, %s signed." % (
                    _plural(lw["dials"], "dial"), _plural(lw["leads"], "lead"), _plural(lw["appointments"], "appointment"),
                    _plural(lw["contracts"], "contract")))
            lines.append(self._month_line())
        else:
            word = "Today so far" if self.run == "noon" else "Today final"
            if day and (day.get("dials") or day.get("conversations") or day.get("leads") or day.get("correct")):
                lines.append("%s: %s." % (word, self._day_text(day)))
            lines.append(self._caller_pace_line())
            lines.append(self._month_line())
        parts = []
        tr = f.get("texts_released_month")
        if isinstance(tr, int) and tr:
            parts.append("Texting: %s released by the autopilot this month" % _plural(tr, "text"))
        ab = f.get("agency_leads_back")
        if isinstance(ab, int):
            parts.append("Agency: %s back from this month's file" % _plural(ab, "lead"))
        if parts:
            lines.append(". ".join(parts) + ".")
        parts = []
        if isinstance(f.get("speed_median"), int):
            parts.append("Speed to lead: median %s." % _plural(f["speed_median"], "minute"))
        if isinstance(f.get("no_task"), int) and f["no_task"]:
            parts.append("%s no next task." % ("1 lead has" if f["no_task"] == 1 else "%s leads have" % kpi.num(f["no_task"])))
        if parts:
            lines.append(" ".join(parts))
        parts = []
        if isinstance(f.get("cards_due"), int) and f["cards_due"]:
            parts.append("%s due a call today" % _plural(f["cards_due"], "county card"))
        if isinstance(f.get("auctions_soon"), int) and f["auctions_soon"]:
            parts.append("%s inside %d days" % (_plural(f["auctions_soon"], "hot auction record"), int(f.get("auction_days") or 14)))
        if parts:
            lines.append("On deck: %s." % ", ".join(parts))
        parts = []
        staged = int(f.get("staged_batches") or 0)
        if staged:
            parts.append("%s waiting to be released" % _plural(staged, "staged batch", "staged batches"))
        if failed:
            parts.append("the %s tab%s could not be built" % (", ".join(failed), "" if len(failed) == 1 else "s"))
        if parts:
            lines.append("Needs a person: %s." % "; ".join(parts))
        if self.run == "evening":
            al = [a for a in f.get("alarms") or [] if not CALLER_ID.search(a)]
            if al:
                lines.append("Alarms today: %s." % "; ".join(al[:2]))
        out = []
        for ln in lines:
            c = voice.clean(ln)
            if not c:
                continue
            if PHONE_SHAPE.search(c):
                self.problem("a Slack line of the KPI tracker looked like it carried a phone number, so it was left out")
                continue
            out.append(c)
        return out

    def _day_text(self, day: dict) -> str:
        text = "%s, %s, %s, %s" % (_plural(day["dials"], "dial"), _plural(day["conversations"], "conversation"),
                                   _plural(day["correct"], "correct number"), _plural(day["leads"], "new lead"))
        who = ["%s %s" % (n, kpi.num(c)) for n, c in sorted((day.get("credited") or {}).items(), key=lambda kv: (-kv[1], kv[0])) if c]
        if day.get("unplaced"):
            who.append("%s credited to nobody" % kpi.num(day["unplaced"]))
        if who and day["leads"]:
            text += " (%s)" % ", ".join(who)
        return text

    def _month_line(self) -> str:
        """The pace line: always shown, zeros and all."""
        m = self.facts.get("month") or {}
        marks = self.facts.get("marks")
        if not m:
            return "Month so far: not read this run (the Scoreboard tab could not be built)."
        pace = []
        for k, word in (("leads", "leads"), ("contracts", "contracts"), ("dials", "dials")):
            v = (marks or {}).get(k)
            if v and v != "not judged yet":
                pace.append("%s %s" % (word, v))
        tail = ("Pace: %s." % ", ".join(pace)) if pace else "Pace: not judged yet."
        return "Month so far: %s, %s, %s closed. %s" % (_plural(m["leads"], "lead"), _plural(m["contracts"], "contract"),
                                                       kpi.num(m["closed"]), tail)

    def _caller_pace_line(self) -> str:
        """The noon and evening pace line: always shown."""
        cat = self.cat
        floor = cat.value("caller.dials_per_day")
        callers = [(n, d) for n, d in (self.facts.get("day") or {}).get("callers") or [] if d]
        if self.run == "noon":
            if not callers:
                return "Pace by noon: no dials yet (%s a caller by end of day)." % kpi.num(floor)
            return "Pace by noon: %s (%s by end of day)." % (
                ", ".join("%s %s" % (n, _plural(d, "dial") if i == 0 else kpi.num(d)) for i, (n, d) in enumerate(callers)),
                kpi.num(floor))
        risk = cat.share("goal.at_risk_share")
        if not callers:
            return "Dials today: none (the floor is %s)." % cat.text("caller.dials_per_day")
        return "Dials today: %s; the floor is %s." % (
            ", ".join("%s %s (%s)" % (n, kpi.num(d), kpi.mark(d, floor, risk)) for n, d in callers), cat.text("caller.dials_per_day"))

    # ---- the rest of what the Slack lines read
    def extra_facts(self) -> None:
        self.facts["staged_batches"] = len(ledger.batches(status="staged"))
        st = self.stage_of("auction", self.o["auction_stage"])
        days = int(st.get("critical_days", 14)) if st is not None else 14
        self.facts["auction_days"] = days
        soon = 0
        limit = self.today + timedelta(days=days)
        with store._LOCK, store._db() as c:
            rows = c.execute("SELECT value FROM ap_facts WHERE key='auction_dates'").fetchall()
        for r in rows:
            try:
                dates = json.loads(r["value"] or "{}")
            except ValueError:
                continue
            ahead, _ = auctiontasks._split(dates if isinstance(dates, dict) else {}, self.today)
            if ahead and ahead[0] <= limit:
                soon += 1
        self.facts["auctions_soon"] = soon
        if self.run == "evening":
            from . import alarms
            self.facts["alarms"] = [r["what"] for r in alarms.raised(day=self.today.isoformat())]


def d90_days(cat) -> int:
    return int(cat.value("goal.deal_window_days"))


def dealsource_researched(tags, w: Win) -> dict:
    from . import researchstats
    return researchstats.researched(tags, since=w.start, until=w.end)


def research_checks() -> list[dict]:
    from . import researchstats
    return researchstats.checks()


def research_table(rows: list[dict]) -> dict:
    from . import researchstats
    return researchstats.table(rows)


# ---------------------------------------------------------------------- building, keeping, publishing
# Which tab gives each fact the Slack lines read.
FACT_TABS = {"day": "Today", "speed_median": "Today", "no_task": "Today", "marks": "Scoreboard", "month": "Scoreboard",
             "last_week": "Scoreboard", "texts_released_month": "Scoreboard", "cards_due": "First to Market",
             "agency_leads_back": "Channels"}
BUILDERS = {"Scoreboard": "tab_scoreboard", "Today": "tab_today", "Callers": "tab_callers", "Texts": "tab_texts",
            "Leads": "tab_leads",
            "Deals": "tab_deals", "First to Market": "tab_ftm", "Direct Mail": "tab_mail", "Channels": "tab_channels",
            "Lists": "tab_lists", "Goals": "tab_goals", "Costs": "tab_costs", "How it is counted": "tab_counted"}


def _load(key: str) -> dict:
    try:
        v = json.loads(store.kv_get(key) or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def latest() -> dict:
    """The last snapshot this stage kept, or {}."""
    return _load(KV_LATEST)


def build(ctx: Ctx, cat, o: dict, run: str) -> tuple[dict, dict]:
    """(snapshot, the facts its Slack lines read). Every tab is built on its own; a tab not due at this
    run is carried from the last snapshot when that one built it, and rebuilt otherwise."""
    s = Snap(ctx, cat, o, run)
    if cat is not None:
        try:
            s.check_stage_map()
        except Exception as exc:  # noqa: BLE001  (a check of the sources' settings never stops the sheet)
            s.problem("the DataSift source's stage map could not be checked (%s)" % str(exc)[:100])
    prev = latest()
    prev_tabs = {t.get("name"): t for t in prev.get("tabs") or [] if isinstance(t, dict)}
    built = _load(KV_BUILT)
    tab_runs = o.get("tab_runs") or {}
    tabs, failed, kept = [], [], []
    for name in TAB_ORDER:
        due = run in (tab_runs.get(name) or RUNS)
        old = prev_tabs.get(name)
        if not due and old and not old.get("failed") and built.get(name):
            t = dict(old)
            t["note"] = ("%s Built at %s; this tab is rebuilt at the %s run." % (
                str(old.get("note") or "").split(" Built at ")[0], built[name], " and ".join(tab_runs.get(name) or RUNS))).strip()
            tabs.append(t)
            kept.append(name)
            continue
        t = s.guarded(name, getattr(s, BUILDERS[name]))
        tabs.append(t)
        if t["failed"]:
            failed.append(name)
        else:
            built[name] = "%s %s" % (s.local.strftime("%a %b %d").replace(" 0", " "), _clock(s.local))
    if cat is not None:
        try:
            s.extra_facts()
        except Exception as exc:  # noqa: BLE001  (the Slack extras must not lose the sheet)
            s.problem("the KPI tracker's on-deck and alarm figures could not be read (%s)" % str(exc)[:100])
    # A fact a CARRIED tab would have given comes from the last run that built it, so the Slack number is
    # the one that tab shows. A tab that failed gives nothing: its line is dropped, never filled with an
    # old number dressed as today's.
    old_facts = _load(KV_FACTS)
    for k, v in old_facts.items():
        if FACT_TABS.get(k) in kept:
            s.facts.setdefault(k, v)
    if cat is None:
        lines = [voice.clean("KPI tracker refreshed %s at %s." % (s.local.strftime("%a, %b %d").replace(" 0", " "),
                                                                   _clock(s.local))),
                 "Month so far: not read this run (the KPI catalog, team/kpi.yaml, does not load).",
                 "Needs a person: the KPI catalog has a problem; every tab keeps its last good version."]
    else:
        lines = s.slack_lines(failed)
    notes = list(s.notes)
    if cat is not None:
        for g, types in (("call", activity.CALLS), ("status", ("property.status.updated",))):
            r = kpi.log_reach(types)
            notes.append("The %s log copy starts %s." % (g, r.astimezone(s.zone).date().isoformat() if r else "nowhere yet"))
        notes.append(s.texts_note())
        notes.append(s.offers_note())
        notes.append(footnote(cat, USES))
        notes += [n for n in s.source_notes if n not in notes]
    if kept:
        notes.append("Kept from an earlier run: %s." % ", ".join(kept))
    problems = list(dict.fromkeys(list(ctx.out.problems) + s.problems))
    snap = {"refreshed": ctx.now.isoformat(timespec="seconds"), "run": run, "tz": s.tz, "tabs": tabs,
            "slack": {"lines": lines}, "problems": problems, "notes": notes, "goal_cells": s.goal_cells_safe()}
    facts = dict(s.facts)
    facts["_built"] = built
    facts["_failed"] = failed
    facts["_kept"] = kept
    facts["_counts_used"] = int(o["max_counts"]) - s.counts_left
    return snap, facts


def remember(snap: dict, facts: dict) -> None:
    """The latest snapshot, which tab was built when, and the facts the Slack lines read, in the kv
    store: the next run carries a tab from here, and Quinn's answers can read the same numbers."""
    # An act or a tally is written as its own plain form (to_dict), never as its printed self.
    store.kv_set(KV_LATEST, json.dumps(snap, default=_jsonable))
    store.kv_set(KV_BUILT, json.dumps(facts.get("_built") or {}, default=_jsonable))
    keep = {k: v for k, v in facts.items() if not k.startswith("_")}
    store.kv_set(KV_FACTS, json.dumps(keep, default=_jsonable))


def publish(ctx: Ctx, snap: dict) -> None:
    """Hand the snapshot to the sheet writer and the Slack poster. Until kpisinks is built, say so and stop."""
    sinks = __name__.rsplit(".", 1)[0] + ".kpisinks"
    try:
        from .kpisinks import publish as sink_publish
    except ImportError as exc:
        missing = getattr(exc, "name", "") or ""
        if missing == sinks or "kpisinks" in str(exc):
            ctx.out.tally["not published: the sheet and Slack sinks (kpisinks) are not built yet"] = 1
            return
        ctx.out.problem("the KPI tracker's sinks could not be loaded, so the sheet and Slack were not updated (%s)" % str(exc)[:120])
        return
    try:
        res = sink_publish(ctx, snap) or {}
    except Exception as exc:  # noqa: BLE001  (a sheet that cannot be written must be said, not crash the run)
        log.warning("kpi tracker publish failed: %s", str(exc)[:200])
        ctx.out.problem("the KPI tracker could not update the sheet and Slack this run (%s)" % str(exc)[:120])
        return
    for p in res.get("problems") or []:
        ctx.out.problem(str(p))
    if res.get("sheet"):
        ctx.out.tally["sheet updated"] = 1
    if res.get("slack"):
        ctx.out.tally["slack: %s" % str(res["slack"])[:60]] = 1


def read_sheet_inputs(ctx: Ctx) -> dict:
    """The Google Sheet's Goals tab, read before anything is counted, so the goal, the targets, the costs
    and the deals typed there are what this run judges by (kpi.load() lays them over team/kpi.yaml).
    Best effort: a sheet that cannot be read leaves the last read, and that is said."""
    try:
        from .kpisinks import read_inputs
    except ImportError:
        return {}
    try:
        res = read_inputs() or {}
    except Exception as exc:  # noqa: BLE001  (the sheet's inputs never stop the run)
        ctx.out.problem("the KPI sheet's Goals tab could not be read (%s)" % str(exc)[:100])
        return {}
    for p in res.get("problems") or []:
        ctx.out.problem(str(p))
    if res.get("status") == "read":
        ctx.out.tally["inputs read from the sheet: %d typed" % int(res.get("typed") or 0)] = 1
    return res


def run_kpitracker(ctx: Ctx) -> None:
    o = options(ctx.stage)
    run = which_run(ctx.stage, ctx.now, ctx.policy.tz)
    sheet_in = read_sheet_inputs(ctx)
    cat = kpi.for_stage(ctx)
    if o["sync"] and cat is not None:
        try:
            kpi.sync_log(ctx, o["groups"])
        except budget.BudgetExhausted as exc:
            ctx.out.problem("the KPI tracker stopped reading the log copy: the hour's request budget ran out (%s); "
                            "the numbers may be short" % str(exc)[:80])
    snap, facts = build(ctx, cat, o, run)
    snap["notes"] += [str(n) for n in sheet_in.get("notes") or [] if str(n) not in snap["notes"]]
    kpi.save_report("kpi_tracker", ctx.now, snap)
    remember(snap, facts)
    summarize(ctx, snap, facts)
    publish(ctx, snap)


def summarize(ctx: Ctx, snap: dict, facts: dict) -> None:
    """What `autopilot last` shows: a short head and the Slack lines. The stage acts on nothing."""
    built = [t["name"] for t in snap["tabs"] if not t.get("failed")]
    failed, kept = facts.get("_failed") or [], facts.get("_kept") or []
    day = facts.get("day") or {}
    ctx.out.found = len(built) - len(kept)
    ctx.out.acted = 0
    ctx.out.tally["tabs built"] = len(built) - len(kept)
    if kept:
        ctx.out.tally["tabs kept from an earlier run"] = len(kept)
    if failed:
        ctx.out.tally["tabs that could not be built"] = len(failed)
    ctx.out.tally["live counts read"] = int(facts.get("_counts_used") or 0)
    bits = ["%d of %d tabs built" % (len(built) - len(kept), len(TAB_ORDER))]
    if kept:
        bits.append("%d kept" % len(kept))
    if failed:
        bits.append("%d failed (%s)" % (len(failed), ", ".join(failed)))
    if day:
        bits.append("%s on %s" % (_plural(_int(day.get("dials")), "dial"), day.get("label") or "the day"))
    ctx.out.head = "KPI tracker, %s run: %s." % (snap["run"], "; ".join(bits))
    for ln in (snap.get("slack") or {}).get("lines") or []:
        ctx.out.line(ln)


# ---------------------------------------------------------------------- the preview (nothing is published)
def render_markdown(snap: dict) -> str:
    """Every tab and the Slack lines as plain text, for a person to read before anything is published."""
    def esc(v) -> str:
        return str(v).replace("|", "/").replace("\n", " ")

    out = ["# KPI tracker preview", "", "Refreshed %s, the %s run (%s). Nothing was published." % (
        snap.get("refreshed"), snap.get("run"), snap.get("tz")), "", "## Slack", ""]
    out += [ln for ln in (snap.get("slack") or {}).get("lines") or []] + [""]
    for t in snap.get("tabs") or []:
        out += ["## %s%s" % (t["name"], " (could not be built)" if t.get("failed") else ""), ""]
        if t.get("note"):
            out += [t["note"], ""]
        cols = t.get("columns") or []
        width = max([len(cols)] + [len(r) for r in t.get("rows") or []] + [1])
        head = [esc(c) for c in cols] + [""] * (width - len(cols))
        out.append("| " + " | ".join(head) + " |")
        out.append("|" + "---|" * width)
        for r in t.get("rows") or []:
            cells = [esc(c) for c in r] + [""] * (width - len(r))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")
    if snap.get("problems"):
        out += ["## Problems", ""] + ["- %s" % p for p in snap["problems"]] + [""]
    if snap.get("notes"):
        out += ["## Notes", ""] + ["- %s" % n for n in snap["notes"]] + [""]
    text = "\n".join(out)
    for ch in (chr(0x2014), chr(0x2013)):
        text = text.replace(ch, "-")
    return text


class _Offline:
    """The CRM for a preview asked to read nothing: every live count reads 'not read'."""
    calls = 0

    def __getattr__(self, name):
        raise RuntimeError("the preview was asked to read nothing from DataSift")


def preview(stage_id: str = "kpi_tracker", out: str = "", run: str = "", offline: bool = False, sync: bool = True,
            now: datetime | None = None, crm=None, pol=None) -> dict:
    """Build a snapshot exactly as the stage would and write it as text; publish nothing."""
    from . import policy as policy_mod
    pol = pol or policy_mod.load()
    if pol is None:
        raise SystemExit("this install has no team/autopilot.yaml")
    st = (pol.stages or {}).get(stage_id) or policy_mod.Stage(
        id=stage_id, kind="kpitracker", owner="ops", mode="plan", summary="",
        schedule=["07:45 Mon-Fri", "12:30 Mon-Fri", "19:30 Mon-Fri"], presets=[], params={})
    if run:
        st = policy_mod.Stage(id=st.id, kind=st.kind, owner=st.owner, mode=st.mode, summary=st.summary,
                              schedule=list(st.schedule), presets=list(st.presets), params=dict(st.params, run=run))
    if crm is None:
        if offline:
            crm = _Offline()
        else:
            from .crm import CRM
            crm = CRM()
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="preview-%s" % uuid_mod.uuid4().hex[:8],
              now=now or datetime.now(timezone.utc), live=False,
              out=Summary(stage=st.id, kind="kpitracker", owner=st.owner, mode="plan", live=False))
    o = options(st)
    o["_offline"] = bool(offline)
    run_name = which_run(st, ctx.now, pol.tz)
    cat = kpi.for_stage(ctx)
    if sync and not offline and o["sync"] and cat is not None:
        kpi.sync_log(ctx, o["groups"])
    snap, _ = build(ctx, cat, o, run_name)
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_markdown(snap), encoding="utf-8")
    return snap


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agents.autopilot.kpitracker",
                                 description="The KPI tracker: build the snapshot without publishing it.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preview", help="write every tab and the Slack lines as text; nothing is published")
    p.add_argument("--out", required=True, help="the file to write (Markdown)")
    p.add_argument("--run", choices=RUNS, help="force the morning, noon or evening view")
    p.add_argument("--stage", default="kpi_tracker", help="the playbook stage whose settings to use")
    p.add_argument("--offline", action="store_true", help="read nothing from DataSift: live counts read 'not read'")
    p.add_argument("--no-sync", action="store_true", help="do not refresh the log copy first")
    args = ap.parse_args(argv)
    snap = preview(args.stage, args.out, args.run or "", offline=args.offline, sync=not args.no_sync)
    built = sum(1 for t in snap["tabs"] if not t.get("failed"))
    print("Wrote %s: %d of %d tabs, the %s run, %d Slack line(s)." % (
        args.out, built, len(snap["tabs"]), snap["run"], len(snap["slack"]["lines"])))
    for ln in snap["slack"]["lines"]:
        print("  " + ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
