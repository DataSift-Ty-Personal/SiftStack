"""The quarterly business audit, scored ahead of time (challenge rule C13.59, Day 5).

The course's company audit asks twenty questions in four sections (marketing, sales management,
operations, KPIs), each rated 1 to 5, so each section scores out of 25: 5 to 10 is ugly, 11 to 15
bad, 16 to 20 good, 21 to 25 excellent. Fix what is critical first, in that section order, because
a sales process means nothing with no leads to talk to. Repeat it every quarter, or every month
while starting out. Nobody ran it: it needed an owner with a free afternoon and a spreadsheet, and
the honest answer to most questions was sitting in the account's own log.

So this stage answers every question the account can answer, from data the brain already holds,
and shows its evidence: the shared log copy (records created, status changes, calls, tasks, notes),
the daily capture, the KPI catalog, the reports the measurement stages save (the goal sheet, the
lost-deal audit) and the deal-source table. A capped number of record reads joins a record to its
owner, and the facts other stages kept are used first. The questions only a person can answer (core
values, SOPs, flow charts, how smooth a handoff feels) are listed for the owner, who types a 1 to 5
for any question under `answers` in this stage's block; an answer also overrides a score from the
data, and the line says both.

Three rules keep the scores honest:

  * A TRUE 5 IS RARE (the course challenges anyone who rates a 5), so a score read from the data is
    capped at `max_data_score` (4). Where the data can see only half of a question (a review a
    person must do, a report that may go unread) the score is capped lower, and the evidence says why.
  * A score needs enough to stand on: a question with too little history in the log copy is not
    guessed, it goes to the owner with what the data does show.
  * The window is said. The log copy reaches back 45 days when it is first built and keeps 200, so
    an early audit reads less than a quarter, and the report says how much it read.

The questions are written here in short words of our own (`labels` changes any one). The fifth KPI
question is not read aloud in the Day 5 session; it is on the audit sheet the workbook links to, so
it waits on the owner until its wording is typed under `labels`.

When it runs: the scheduler has no quarterly slot, so the stage sits on a monthly one and does the
full audit only in a period it has not audited yet (`every_months`: 3 is quarterly, 1 monthly).
Only a run the clock started marks the period done; an ask from Slack or the command line always
answers and never uses up the period's audit. It reads only and writes nothing to the account; the
full detail is kept as a report file on the box's own volume.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import config, store
from . import activity, budget, capture, kpi, lanes
from . import leadroutes as routes
from .accountchecks import owner_of
from .stages import Ctx

log = logging.getLogger(__name__)

SECTIONS = (("marketing", "Marketing"), ("sales", "Sales management"), ("operations", "Operations"), ("kpis", "KPIs"))
SHORT = {"marketing": "marketing", "sales": "sales management", "operations": "operations", "kpis": "KPIs"}
# Twenty questions, five a section, in the course's order. Short words of our own for each.
QUESTIONS = (
    ("M1", "marketing", "New data comes in on a set routine and runs through marketing"),
    ("M2", "marketing", "Each record shows whether it was marketed to, and how"),
    ("M3", "marketing", "Channels are stacked cheapest first, without hitting owners already reached"),
    ("M4", "marketing", "Leads come in steadily from a niche the business has mastered"),
    ("M5", "marketing", "Owners who did not answer earlier marketing are worked again"),
    ("S1", "sales", "New leads are reached fast and qualified"),
    ("S2", "sales", "Every lead has a next task, done on time for its temperature"),
    ("S3", "sales", "Handoffs between lead management, acquisitions, transactions and dispo are smooth"),
    ("S4", "sales", "Every offer and seller conversation is written in the CRM"),
    ("S5", "sales", "Leads and deals are checked for what was lost"),
    ("O1", "operations", "The net margin is known and healthy"),
    ("O2", "operations", "The team works by the mission, vision and core values"),
    ("O3", "operations", "Everyone can say their daily, weekly and monthly tasks"),
    ("O4", "operations", "The team knows where the SOPs are and uses them"),
    ("O5", "operations", "Flow charts are kept up to date"),
    ("K1", "kpis", "KPIs are measured against set goals"),
    ("K2", "kpis", "KPIs are reviewed by role, on a schedule"),
    ("K3", "kpis", "The numbers are used to find and fix what is off"),
    ("K4", "kpis", "Each person's day is tracked as a scorecard"),
    ("K5", "kpis", "The fifth KPI question on the audit sheet"),
)
IDS = tuple(q[0] for q in QUESTIONS)
BANDS = ((21, "excellent"), (16, "good"), (11, "bad"), (5, "ugly"))
# A note a person wrote. internal-api is NOT a machine here: a person in the app is logged that way.
MACHINE_NOTE_SOURCES = {"sequence", "bulk", "auto-update", "data-enriching", "system", "import"}
NO_TASK_NEEDED = ("under_contract", "transactions", "push_deal", "refer_lead")

DEFAULTS = {
    "every_months": 3,            # quarterly; 1 while starting out (C13.59)
    "window_days": 91,
    "max_data_score": 4,          # a true 5 is rare: the data never claims one, the owner can
    "max_record_reads": 30,       # per question that joins records to owners
    "min_sample": 10,             # records or conversations under a share before it is scored
    "min_days": 10,               # business days of history before a daily rate is scored
    "min_weeks": 3,
    "min_leads": 5,
    "min_created": 20,
    "ni_follow_days": 30,         # a Not Interested owner is due a call back after this long
    "note_hours": 24,             # a note this long after a conversation documents it
    "conversation_seconds": 60,   # used when the KPI catalog does not load
    "sample_conversations": 40,
    "weekly_leads_floor": None,   # default: five times the low end of caller.opportunities_per_day
    "max_scan": 2000,
    "fix_first": 3,
    "sync": True,
    "max_log_calls": 300,
}


@dataclass
class Result:
    qid: str
    section: str
    label: str
    score: int | None = None
    source: str = ""              # data, answer, or "" while it waits on the owner
    evidence: str = ""
    data_score: int | None = None


def setting(st, key: str):
    v = st.get(key)
    return DEFAULTS.get(key) if v is None else v


def band(x: float, cuts) -> int:
    """The score for a share or a count where more is better: cuts are (at least, score), best first."""
    for at_least, score in cuts:
        if x >= at_least:
            return score
    return 1


def band_low(x: float, cuts) -> int:
    """The same where less is better (minutes to a first call): cuts are (at most, score), best first."""
    for at_most, score in cuts:
        if x <= at_most:
            return score
    return 1


def section_band(total: int) -> str:
    for at_least, name in BANDS:
        if total >= at_least:
            return name
    return "ugly"


def period(day: date, every: int) -> tuple[str, str]:
    """(the key of the audit period this day is in, how a person says it)."""
    every = max(1, min(12, int(every)))
    idx = (day.month - 1) // every
    first = idx * every + 1
    key = "%d-%02d/%d" % (day.year, first, every)
    if every == 3:
        return key, "Q%d %d" % (idx + 1, day.year)
    if every == 1:
        return key, "%s %d" % (date(day.year, first, 1).strftime("%b"), day.year)
    last = min(12, first + every - 1)
    return key, "%s to %s %d" % (date(day.year, first, 1).strftime("%b"), date(day.year, last, 1).strftime("%b"), day.year)


def _scheduled(ctx: Ctx) -> bool:
    """True when the clock started this run (only then is the period marked as audited)."""
    if not ctx.task_id:
        return False
    t = store.task(ctx.task_id) or {}
    return str(t.get("source") or "") == "schedule"


def _business_days(start: date, end: date) -> list[str]:
    """Weekdays from start up to, not including, end, as ISO days."""
    out, d = [], start
    while d < end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _dur(minutes: float) -> str:
    if minutes < 90:
        return "%d min" % round(minutes)
    if minutes < 48 * 60:
        return "%.1f hours" % (minutes / 60.0)
    return "%.1f days" % (minutes / 1440.0)


def _fmt(day) -> str:
    return kpi.fmt_day(day)


class Env:
    """What every scorer reads: the window, the business days, the catalog, and a read budget."""

    def __init__(self, ctx: Ctx, cat):
        self.ctx, self.cat, self.st = ctx, cat, ctx.stage
        self.tz = ctx.policy.tz
        self.today = date.fromisoformat(kpi.local_day(ctx.now, self.tz))
        self.today_start = kpi.day_start(self.today, self.tz)
        self.days = int(setting(self.st, "window_days"))
        self.start = self.today_start - timedelta(days=self.days)

    def held_from(self, types) -> datetime | None:
        """How far back the log copy answers for these types, inside the window."""
        reach = kpi.log_reach(types)
        if reach is None:
            return None
        return max(self.start, reach)

    def reads(self) -> dict:
        return {"left": int(setting(self.st, "max_record_reads"))}

    def figure_low(self, key: str, fallback: float) -> float:
        try:
            return float(self.cat.low(key)) if self.cat is not None else fallback
        except Exception:  # noqa: BLE001  (a figure the catalog lacks falls back, it does not stop the audit)
            return fallback


# ---------------------------------------------------------------------- the scorers, one per question the data can answer
def score_m1(env: Env) -> tuple[int | None, str]:
    """New data on a routine: the share of business days on which new records landed."""
    held = env.held_from(activity.STATE)
    if held is None:
        return None, "the log copy holds no record changes yet"
    days = _business_days(held.astimezone(kpi.zone(env.tz)).date(), env.today)
    if len(days) < int(setting(env.st, "min_days")):
        return None, "the log copy holds %d business day(s); %d are needed" % (len(days), int(setting(env.st, "min_days")))
    landed = {kpi.local_day(e["when"], env.tz) for e in activity.events("property.created", since=held, until=env.today_start)}
    n = len(landed & set(days))
    share = n / float(len(days))
    return band(share, ((0.97, 5), (0.9, 4), (0.7, 3), (0.4, 2))), (
        "new records landed on %d of %d business days since %s. This reads the inflow, not whether each new record "
        "reached its first touch" % (n, len(days), _fmt(days[0])))


def score_m4(env: Env) -> tuple[int | None, str]:
    """Steady leads: the share of full weeks with at least the weekly floor of new leads."""
    held = env.held_from(("property.status.updated",))
    if held is None:
        return None, "the log copy holds no status changes yet"
    first = held.astimezone(kpi.zone(env.tz)).date()
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    this_monday = env.today - timedelta(days=env.today.weekday())
    weeks = []
    d = monday
    while d < this_monday:
        weeks.append(d)
        d += timedelta(days=7)
    if len(weeks) < int(setting(env.st, "min_weeks")):
        return None, "the log copy holds %d full week(s); %d are needed" % (len(weeks), int(setting(env.st, "min_weeks")))
    floor = setting(env.st, "weekly_leads_floor")
    floor = float(floor) if floor is not None else 5 * env.figure_low("caller.opportunities_per_day", 1.0)
    counts = [len(kpi.new_leads(since=kpi.day_start(w, env.tz), until=kpi.day_start(w + timedelta(days=7), env.tz)))
              for w in weeks]
    at = sum(1 for c in counts if c >= floor)
    score = 1 if not sum(counts) else band(at / float(len(weeks)), ((0.95, 5), (0.8, 4), (0.5, 3), (0.25, 2)))
    return score, ("new leads a week, last %d weeks: %s, against a floor of %s (%d week(s) at it). Whether they come "
                   "from one mastered niche is yours to judge" % (
                       len(weeks), ", ".join(str(c) for c in counts[-8:]), kpi.num(round(floor)), at))


def score_m5(env: Env) -> tuple[int | None, str]:
    """Non-responders worked again: Not Interested records that were called or texted again once
    their wait was over."""
    follow = int(setting(env.st, "ni_follow_days"))
    grace = 5
    changes = kpi.status_changes(["not_interested"], since=env.start, until=env.ctx.now - timedelta(days=follow + 7))
    need = int(setting(env.st, "min_sample"))
    if len(changes) < need:
        return None, "%d record(s) went Not Interested long enough ago to judge (%d needed)" % (len(changes), need)
    reads = env.reads()
    judged = again = 0
    for c in changes:
        owner = owner_of(env.ctx, c["record"], reads)
        if not owner:
            continue
        judged += 1
        after = c["when"] + timedelta(days=follow - grace)
        dials = [e for e in activity.events(activity.DIAL, resource=owner, since=after) if e["data"].get("direction") != "inbound"]
        texts = [e for e in activity.events("owner.sms.sent", resource=owner, since=after) if e["data"].get("direction") != "inbound"]
        if dials or texts:
            again += 1
    if judged < need:
        return None, "only %d of %d Not Interested record(s) could be joined to their owner inside the read cap" % (judged, len(changes))
    share = again / float(judged)
    return band(share, ((0.8, 5), (0.6, 4), (0.3, 3), (0.1, 2))), (
        "%d of %d records marked Not Interested at least %d days ago were called or texted again after %d days. Texts sent "
        "through Smarter Contact do not show in the log" % (again, judged, follow + 7, follow - grace))


def score_s1(env: Env) -> tuple[int | None, str]:
    """Speed to lead: minutes from a new lead to the first outbound call to its owner. A status set
    on a call has no wait to measure; a lead nobody called is counted on its own."""
    leads = kpi.new_leads(since=env.start, until=env.ctx.now - timedelta(hours=1))
    reads = env.reads()
    waits, never, on_call = [], 0, 0
    for ld in leads:
        owner = owner_of(env.ctx, ld["record"], reads)
        if not owner:
            continue
        calls = [e for e in activity.events(activity.CALLS, resource=owner, since=ld["when"] - timedelta(minutes=20))
                 if e["event_type"] != "owner.call.missed"]
        if any(e["when"] <= ld["when"] for e in calls):
            on_call += 1
            continue
        dials = [e for e in calls if e["data"].get("direction") != "inbound" and e["event_type"] == activity.DIAL]
        if dials:
            waits.append((dials[0]["when"] - ld["when"]).total_seconds() / 60.0)
        else:
            never += 1
    judged = len(waits) + never
    if judged < int(setting(env.st, "min_leads")):
        return None, "%d new lead(s) with a wait to measure (%d needed)%s" % (
            judged, int(setting(env.st, "min_leads")), "; %d were set on a call" % on_call if on_call else "")
    if waits:
        waits.sort()
        median = waits[len(waits) // 2]
        score = band_low(median, ((5, 5), (60, 4), (240, 3), (1440, 2)))
        text = "median %s from a new lead to the first call over %d lead(s)" % (_dur(median), len(waits))
    else:
        score, text = 1, "none of %d new lead(s) has been called" % judged
    if never and never / float(judged) > 0.25:
        score = min(score, 2)
    return score, "%s; %d never called, %d set on a call (no wait to measure)" % (text, never, on_call)


def score_s2(env: Env) -> tuple[int | None, str]:
    """A next task, done on time: leads holding an open task, and the share of tasks made that were
    completed. The lower of the two is the score: a task that exists and is never done is no next
    action."""
    st = env.st
    parts, scores = [], []
    statuses = [s for s in lanes.LEAD_STATUS_VALUES if s.lower() not in NO_TASK_NEEDED]
    rows = routes.status_rows(env.ctx, statuses, cap=int(setting(st, "max_scan")))
    counted = [r for r in rows if isinstance(r.get("tasks"), dict)]
    if counted:
        have = sum(1 for r in counted if int((r.get("tasks") or {}).get("incomplete") or 0) > 0)
        share = have / float(len(counted))
        scores.append(band(share, ((0.95, 5), (0.85, 4), (0.7, 3), (0.5, 2))))
        parts.append("%d of %d leads hold an open task" % (have, len(counted)))
    held = env.held_from(activity.BOARD)
    if held is not None:
        made = activity.events("task.created", since=held)
        done = activity.events("task.completed", since=held)
        gone = [e for e in activity.events("task.deleted", since=held) if str(e.get("source") or "").lower() == "sequence"]
        if len(made) >= int(setting(st, "min_created")):
            share = len(done) / float(len(made))
            scores.append(band(share, ((0.7, 5), (0.5, 4), (0.3, 3), (0.1, 2))))
            parts.append("%d of %d tasks made since %s were completed%s" % (
                len(done), len(made), _fmt(kpi.local_day(held, env.tz)), " (%d deleted by a sequence)" % len(gone) if gone else ""))
    if not scores:
        return None, "neither the lead rows nor the task log hold enough to judge"
    return min(scores), "; ".join(parts)


def _person_note(e: dict) -> bool:
    # This audit always read an internal-api note as a person's; it still does for rows stored before
    # the log copy told our own key apart, and never for our key's own notes since.
    return activity.by_person(e, old_internal_api_is_person=True) \
        and str(e.get("source") or "").lower() not in MACHINE_NOTE_SOURCES


def score_s4(env: Env) -> tuple[int | None, str]:
    """Conversations written down: the share of recent calls of a minute or more that a person's
    note followed, on the owner or on one of the owner's records."""
    st = env.st
    hours = float(setting(st, "note_hours"))
    min_s = int(setting(st, "conversation_seconds"))
    if env.cat is not None:
        try:
            min_s = int(env.cat.definition("conversation")["min_seconds"])
        except Exception:  # noqa: BLE001
            pass
    convs = []
    for e in activity.events(("owner.call.answered", "owner.call.received"), since=env.start,
                             until=env.ctx.now - timedelta(hours=hours)):
        if int(e["data"].get("duration") or 0) >= min_s and e["resource"]:
            convs.append(e)
    convs = convs[-int(setting(st, "sample_conversations")):]
    need = int(setting(st, "min_sample"))
    if len(convs) < need:
        return None, "%d call(s) of a minute or more in the log copy (%d needed)" % (len(convs), need)
    reads = env.reads()
    records: dict[str, list] = {}
    judged = noted = 0
    for c in convs:
        lo, hi = c["when"] - timedelta(minutes=5), c["when"] + timedelta(hours=hours)
        owner = c["resource"]
        if any(_person_note(e) for e in activity.events(("owner.message.added",), resource=owner, since=lo, until=hi)):
            judged += 1
            noted += 1
            continue
        if owner not in records:
            if reads["left"] <= 0:
                continue
            reads["left"] -= 1
            try:
                records[owner] = [r.get("uuid") for r in (env.ctx.crm.owner_records(owner, cap=5) or []) if r.get("uuid")]
            except budget.BudgetExhausted:
                reads["left"] = 0
                continue
            except Exception:  # noqa: BLE001
                records[owner] = []
        judged += 1
        if any(_person_note(e) for u in records[owner]
               for e in activity.events(("property.message.added",), resource=u, since=lo, until=hi)):
            noted += 1
    if judged < need:
        return None, "only %d call(s) could be checked for a note inside the read cap" % judged
    share = noted / float(judged)
    return band(share, ((0.9, 5), (0.75, 4), (0.5, 3), (0.25, 2))), (
        "a person's note followed %d of the last %d calls of a minute or more, within %d hours. Offers are not told "
        "apart from other calls in the log" % (noted, judged, round(hours)))


def _reports(prefix: str) -> list[Path]:
    folder = Path(config.DATA_DIR) / "kpi"
    if not folder.is_dir():
        return []
    return sorted(folder.glob("%s*.json" % prefix))


def score_s5(env: Env) -> tuple[int | None, str]:
    """Checked for what was lost: a lost-deal audit report for each of the last months. At most 3,
    because whether a person worked each sale is not in the data."""
    months = []
    d = date(env.today.year, env.today.month, 1)
    for _ in range(3):
        d = (d - timedelta(days=1)).replace(day=1)
        months.append(d.strftime("%Y-%m"))
    have = sorted({p.name[len("lost_deals_"):][:7] for p in _reports("lost_deals_")} & set(months))
    names = ", ".join(date.fromisoformat(m + "-01").strftime("%b") for m in have)
    if not have:
        return 1, "no lost-deal audit report for any of the last three months"
    score = 3 if len(have) >= 2 else 2
    return score, ("the monthly lost-deal audit has a report for %s. Whether a person worked each sale is not in the "
                   "data, so the score stops at 3" % names)


def score_o1(env: Env) -> tuple[int | None, str]:
    """Net margin, from the goal sheet's quarterly owner review (the deals recorded and what the
    autopilot paid). The first two bands are the course's (0% is a 1, up to 20% a 2); the rest go up
    in steps of 20 points, a setting (margin_bands)."""
    if env.cat is None:
        return None, "the KPI catalog did not load, so the net margin was not read"
    from . import goals
    try:
        lines, data = goals.owner_review(env.ctx, env.cat, env.today)
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (the review failing is said, the audit goes on)
        return None, "the net margin could not be read (%s)" % str(exc)[:80]
    said = next((ln for ln in lines if ln.startswith("Net margin")), "")
    if "net_margin" not in data:
        return None, said or "no net margin is recorded"
    cuts = env.st.get("margin_bands") or ((60.01, 5), (40.01, 4), (20.01, 3), (0.01, 2))
    return band(float(data["net_margin"]), tuple(tuple(c) for c in cuts)), said


def score_k1(env: Env) -> tuple[int | None, str]:
    """KPIs against set goals: how settled the goal figures the KPIs are judged against are."""
    if env.cat is None:
        return 1, "no KPI catalog (team/kpi.yaml) loads, so nothing is measured against a goal"
    rank = {"placeholder": 2, "recommended": 3, "measured": 4, "confirmed": 4}
    said, scores = [], []
    for key, name in (("goal.revenue_per_year", "the revenue goal"), ("goal.average_deal", "the average deal")):
        try:
            f = env.cat.fig(key)
        except Exception:  # noqa: BLE001
            scores.append(1)
            said.append("%s is not in the catalog" % name)
            continue
        scores.append(rank.get(str(f.status), 4))
        said.append("%s is %s%s" % (name, f.text(), " (%s)" % f.tag() if f.tag() else ""))
    return min(scores), "; ".join(said)


def score_k2(env: Env) -> tuple[int | None, str]:
    """KPIs reviewed by role: how often the goal sheet, with each caller's numbers, was made in the
    last 35 days. At most 3, because whether someone reads and talks it over is not in the data."""
    since = (env.today - timedelta(days=35)).isoformat()
    n = 0
    for p in _reports("goal_sheet_"):
        day = p.stem[len("goal_sheet_"):][:10]
        if day >= since:
            n += 1
    score = 1 if n == 0 else (2 if n < 4 else 3)
    return score, ("the goal sheet, each caller's numbers against the floors, was made %d time(s) in the last 35 days. "
                   "Whether each role's numbers are read and talked over is not in the data, so the score stops at 3" % n)


def score_k4(env: Env) -> tuple[int | None, str]:
    """Each person's day as a scorecard: business days with a captured calling day."""
    held = env.held_from(activity.CALLS)
    if held is None:
        return None, "the log copy holds no calls yet"
    days = _business_days(held.astimezone(kpi.zone(env.tz)).date(), env.today)
    if len(days) < int(setting(env.st, "min_days")):
        return None, "the log copy holds %d business day(s) of calls; %d are needed" % (len(days), int(setting(env.st, "min_days")))
    got = {r["day"] for r in capture.rows(since_day=days[0])}
    n = len(got & set(days))
    return band(n / float(len(days)), ((0.97, 5), (0.85, 4), (0.6, 3), (0.3, 2))), (
        "a scorecard row (dials, answers, conversations) was written for %d of %d business days. It covers the callers, "
        "not the lead manager's other work" % (n, len(days)))


SCORERS = {"M1": score_m1, "M4": score_m4, "M5": score_m5, "S1": score_s1, "S2": score_s2, "S4": score_s4,
           "S5": score_s5, "O1": score_o1, "K1": score_k1, "K2": score_k2, "K4": score_k4}
# Where a question only a person can answer still has something in the data worth reading first.
HINT_KINDS = {"M3": "audit", "K3": "alarms"}
HINTS = {
    "M2": "open three worked records and check each shows every channel it was marketed through",
    "S3": "ask whether a seller has had to repeat an answer after a handoff",
    "K5": "it is on the audit sheet the Day 5 workbook links to; type its wording under labels",
}


def last_head(ctx: Ctx, kind: str) -> str:
    """What the newest run of a stage of this kind said, with its day ("" when none has run)."""
    best = None
    for s in (getattr(ctx.policy, "stages", None) or {}).values():
        if s.kind != kind:
            continue
        try:
            last = json.loads(store.kv_get("autopilot:last:%s" % s.id) or "{}")
        except ValueError:
            continue
        if last.get("head") and (best is None or str(last.get("at")) > str(best.get("at"))):
            best = last
    if not best:
        return ""
    return "the %s stage last said: %s (%s)" % (kind, best["head"], _fmt(str(best.get("at"))[:10]))


# ---------------------------------------------------------------------- the stage
def _catalog(ctx: Ctx):
    try:
        return kpi.load()
    except kpi.KpiError as exc:
        ctx.out.tally["KPI catalog did not load"] = 1
        log.info("business audit without the KPI catalog: %s", exc)
        return None


def audit(ctx: Ctx, cat) -> list[Result]:
    """Every question, scored from the data where it can be, with the owner's answers laid over."""
    st = ctx.stage
    env = Env(ctx, cat)
    cap = int(setting(st, "max_data_score"))
    labels = st.get("labels") or {}
    answers = st.get("answers") or {}
    out = []
    for qid, section, label in QUESTIONS:
        r = Result(qid=qid, section=section, label=str(labels.get(qid) or label))
        fn = SCORERS.get(qid)
        if fn is not None:
            try:
                score, evidence = fn(env)
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (one question failing must not hide the other nineteen)
                score, evidence = None, "could not be read (%s)" % str(exc)[:80]
            r.evidence = evidence
            if score is not None:
                r.data_score = r.score = max(1, min(int(score), cap))
                r.source = "data"
        else:
            hint = last_head(ctx, HINT_KINDS[qid]) if qid in HINT_KINDS else HINTS.get(qid, "")
            r.evidence = hint
        if qid in answers:
            r.score, r.source = int(answers[qid]), "answer"
        out.append(r)
    return out


def _section_line(section: str, name: str, rs: list[Result]) -> tuple[str, str]:
    """(the section's line, its short form for the head). A section is banded only once all five of
    its questions have a score: a total out of 25 read from three questions would read as bad."""
    scored = [r for r in rs if r.score is not None]
    total = sum(r.score for r in scored)
    each = ", ".join("%s %d" % (r.qid, r.score) for r in scored)
    waiting = [r.qid for r in rs if r.score is None]
    short = SHORT.get(section, name)
    if not waiting:
        return ("%s: %d of 25, %s (%s)." % (name, total, section_band(total), each),
                "%s %d of 25 (%s)" % (short, total, section_band(total)))
    return ("%s: %d so far from %d of 5 questions (%s); %s wait%s on you." % (
        name, total, len(scored), each or "none scored", ", ".join(waiting), "s" if len(waiting) == 1 else ""),
        "%s %d so far" % (short, total))


def run_bizaudit(ctx: Ctx) -> None:
    st = ctx.stage
    tz = ctx.policy.tz
    today = date.fromisoformat(kpi.local_day(ctx.now, tz))
    key, label = period(today, int(setting(st, "every_months")))
    mark = "autopilot:bizaudit:%s" % st.id
    scheduled = _scheduled(ctx)
    if scheduled and store.kv_get(mark) == key:
        ctx.out.tally["already audited for %s" % label] = 1
        return            # no head and no lines: a report with nothing in it posts nothing
    if setting(st, "sync"):
        kpi.sync_log(ctx, ("state", "calls", "board", "notes", "texts"))
    cat = _catalog(ctx)
    results = audit(ctx, cat)
    by_section = {s: [r for r in results if r.section == s] for s, _ in SECTIONS}
    heads, lines = [], []
    for s, name in SECTIONS:
        line, short = _section_line(s, name, by_section[s])
        lines.append(line)
        heads.append(short)
    order = {s: i for i, (s, _) in enumerate(SECTIONS)}
    critical = sorted([r for r in results if r.score is not None and r.score <= 2],
                      key=lambda r: (order[r.section], r.score, r.qid))
    for r in critical[:int(setting(st, "fix_first"))]:
        how = "you scored it" if r.source == "answer" else "from the data"
        extra = ""
        if r.source == "answer" and r.data_score is not None:
            extra = " The data read %d: %s." % (r.data_score, r.evidence)
        elif r.evidence:
            extra = " %s." % (r.evidence[:1].upper() + r.evidence[1:])
        lines.append("Fix first, %s (%d of 5, %s): %s.%s" % (r.qid, r.score, how, r.label, extra))
    waiting = [r for r in results if r.score is None]
    if waiting:
        lines.append("Your answer is needed on %s: type each 1 to 5 under answers in this stage's block." % ", ".join(
            r.qid for r in waiting))
    held = Env(ctx, cat).held_from(activity.STATE)
    window = int(setting(st, "window_days"))
    if held is not None:
        span = max(0, (ctx.now - held).days)
        lines.append("Read from the log copy since %s (%d of the %d days the audit looks at)." % (
            _fmt(kpi.local_day(held, tz)), min(span, window), window))
    else:
        lines.append("The log copy holds nothing yet, so every question that reads it waits.")
    for ln in lines:
        ctx.out.line(ln)
    scored = [r for r in results if r.score is not None]
    ctx.out.found = len(scored)
    ctx.out.acted = len(critical)
    ctx.out.tally.update({"questions scored from the data": sum(1 for r in results if r.source == "data"),
                          "questions answered by the owner": sum(1 for r in results if r.source == "answer"),
                          "questions waiting on the owner": len(waiting), "critical (1 or 2)": len(critical)})
    ctx.out.head = "Business audit for %s: %s.%s" % (label, ", ".join(heads),
                                                    " %d question(s) wait on your answer." % len(waiting) if waiting else "")
    kpi.save_report("business_audit", ctx.now, {
        "period": key, "label": label, "window_days": window,
        "log_copy_from": held.isoformat() if held else None,
        "questions": [{"id": r.qid, "section": r.section, "question": r.label, "score": r.score, "source": r.source,
                       "data_score": r.data_score, "evidence": r.evidence} for r in results],
        "sections": {s: {"total": sum(r.score for r in by_section[s] if r.score is not None),
                         "scored": sum(1 for r in by_section[s] if r.score is not None)} for s, _ in SECTIONS},
        "fix_first": [r.qid for r in critical]})
    if scheduled:
        store.kv_set(mark, key)       # the period's audit is done; an ask from Slack never uses it up


# ---------------------------------------------------------------------- the playbook block
def block_problems(st) -> list[str]:
    """A mistyped setting is refused when the playbook loads, instead of read as a default."""
    p = []
    every = st.get("every_months")
    if every is not None and (isinstance(every, bool) or not isinstance(every, int) or not 1 <= every <= 12):
        p.append("stage %s: every_months must be a whole number from 1 to 12 (3 is quarterly)" % st.id)
    cap = st.get("max_data_score")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 5):
        p.append("stage %s: max_data_score must be a whole number from 1 to 5" % st.id)
    for key in ("window_days", "max_record_reads", "min_sample", "min_days", "min_weeks", "min_leads", "min_created",
                "ni_follow_days", "note_hours", "conversation_seconds", "sample_conversations", "max_scan", "fix_first",
                "max_log_calls"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            p.append("stage %s: %s must be a number of zero or more" % (st.id, key))
    floor = st.get("weekly_leads_floor")
    if floor is not None and (isinstance(floor, bool) or not isinstance(floor, (int, float)) or floor <= 0):
        p.append("stage %s: weekly_leads_floor must be a positive number" % st.id)
    answers = st.get("answers")
    if answers is not None:
        if not isinstance(answers, dict):
            p.append("stage %s: answers must map a question id (M1 to K5) to a score from 1 to 5" % st.id)
        else:
            for k, v in answers.items():
                if str(k) not in IDS:
                    p.append("stage %s: answers names %r, which is not a question (M1 to M5, S1 to S5, O1 to O5, K1 to K5)" % (st.id, k))
                elif isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
                    p.append("stage %s: the answer to %s must be a whole number from 1 to 5" % (st.id, k))
    labels = st.get("labels")
    if labels is not None:
        if not isinstance(labels, dict) or any(str(k) not in IDS or not isinstance(v, str) for k, v in labels.items()):
            p.append("stage %s: labels must map a question id (M1 to K5) to its wording" % st.id)
    bands = st.get("margin_bands")
    if bands is not None and (not isinstance(bands, list) or any(
            not isinstance(b, list) or len(b) != 2 or not all(isinstance(x, (int, float)) for x in b) for b in bands)):
        p.append("stage %s: margin_bands must list [at least percent, score] pairs, best first" % st.id)
    return p
