"""kpi_report: the week's dials, connects, conversations, leads and appointments against the catalog.

Why it is rebuilt here. The kpi-engine skill pulls the account's activity log with a browser token
someone pastes in, which DataSift's published terms say not to build on, and it carried its own copy
of the floors. Everything the report needs is already on the brain: the daily capture's stored days
(ap_capture), the shared copy of the account log (ap_log) and the KPI catalog (team/kpi.yaml). This
reads those and nothing else: no DataSift request, no spend, no token.

What the words mean, the catalog's way (team/kpi.yaml `definitions`, said again in the workbook):
  * a DIAL is an outbound call. ANSWERED is what a dialer calls a connect, and a voicemail greeting
    answers too: on 2026-11-12, 185 of 234 dials were answered and 6 were conversations. So both
    numbers are shown, and only conversations are judged.
  * a CONVERSATION is an answered call of a minute or more, an inbound call that long, or an
    inbound text (kpi.caller_days).
  * a LEAD is a record entering a lead status from outside one (kpi.new_leads). It is credited to
    the caller whose conversation came last before it, using the owner the autopilot already knows
    for that record; one it cannot place is counted for the account and said, never guessed.
  * an APPOINTMENT is an appointment task booked on a record, or a card moved into the Appointment
    Booked column. Until plan 2.10 changes the Make Offer sequence, that column also receives every
    offer card, so the workbook shows the two sources apart.

The week is Monday to Sunday: "last" (the default, what Monday's 8am report reads), "this" (Monday
to today) or any day inside the week wanted. A caller is judged per calling day against the floors,
on a week, which the catalog says is not a test (callers are judged on 90 days: the goal sheet holds
that view). The Slack text is two lines and a link; everything else is in the workbook.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, slack, store

log = logging.getLogger(__name__)

WORKFLOW = "kpi_report"
TZ = "America/New_York"
NOT_CALLERS = ("unknown", "not tied to a caller")
USES = ("caller.dials_per_day", "caller.conversations_per_day", "caller.opportunities_per_day",
        "funnel.dials_per_correct_number", "funnel.dials_per_lead", "goal.at_risk_share", "alarm.answer_rate_normal")
NOW = None          # a fixed "now" for the selftest
EM, EN = chr(0x2014), chr(0x2013)


# ---------------------------------------------------------------------- a plain workbook
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return v
    return str(v).replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-")


def _sheet_title(t: str) -> str:
    t = "".join(ch for ch in str(t) if ch not in "[]:*?/\\")
    return (t or "Sheet")[:31]


def write_workbook(path: Path, sheets) -> Path:
    """One sheet per (title, header, rows): a bold header with a thin rule under it, no colored
    bands, the top row frozen, columns sized to what they hold. Written to a _PENDING_ name and
    swapped, so a reader never opens half a file."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    first = True
    for title, header, rows in sheets:
        ws = wb.active if first else wb.create_sheet()
        first = False
        ws.title = _sheet_title(title)
        ws.append([_cell(h) for h in header])
        for c in ws[1]:
            c.font = Font(bold=True)
            c.border = Border(bottom=Side(style="thin", color="999999"))
            c.alignment = Alignment(vertical="top", wrap_text=True)
        for r in rows:
            ws.append([_cell(v) for v in r])
        for i, col in enumerate(ws.columns, 1):
            longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[get_column_letter(i)].width = min(70, max(9, longest + 2))
            if longest > 70:
                for c in col:
                    c.alignment = Alignment(vertical="top", wrap_text=True)
        ws.freeze_panes = "A2"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------- the week
def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _tz() -> str:
    try:
        from ..autopilot import policy
        pol = policy.load()
        return pol.tz if pol else TZ
    except Exception:  # noqa: BLE001
        return TZ


def _fmt(d: date) -> str:
    return "%s %d" % (d.strftime("%b"), d.day)


def window(week: str, now: datetime, tz: str) -> tuple[date, date, str, str]:
    """(first day, last day, label, problem). Monday to Sunday; a week still running reads to today."""
    today = now.astimezone(ZoneInfo(tz)).date()
    w = " ".join(str(week or "last").split()).lower()
    monday = today - timedelta(days=today.weekday())
    problem = ""
    if w in ("this", "this week", "current", "so far"):
        start, end = monday, today
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", w):
        try:
            d = date.fromisoformat(w)
        except ValueError:
            d = None
        if d is None or d > today:
            problem = "%r is not a day that has happened; last week is shown instead" % week
            start, end = monday - timedelta(days=7), monday - timedelta(days=1)
        else:
            start = d - timedelta(days=d.weekday())
            end = min(start + timedelta(days=6), today)
    else:
        if w not in ("last", "last week", "previous", ""):
            problem = "%r is not a week this report reads (last, this, or a day as YYYY-MM-DD); last week is shown" % week
        start, end = monday - timedelta(days=7), monday - timedelta(days=1)
    return start, end, "%s to %s" % (_fmt(start), _fmt(end)), problem


# ---------------------------------------------------------------------- what the log says
def owner_map() -> dict:
    """record -> owner, from the nightly owner-lane map (no DataSift read). Status changes are logged
    under the record and calls under the owner, so crediting a lead needs this join."""
    import json
    out = {}
    with store._LOCK, store._db() as c:
        try:
            rows = c.execute("SELECT owner_uuid, records FROM ap_owner_lane").fetchall()
        except Exception:  # noqa: BLE001  (an install that never ran the lane map)
            return out
    for r in rows:
        try:
            for rec in (json.loads(r["records"] or "{}") or {}):
                out.setdefault(rec, r["owner_uuid"])
        except ValueError:
            continue
    return out


def credit(cat, leads: list[dict], omap: dict) -> tuple[Counter, int]:
    """caller -> leads credited to them, and how many no caller could be named for."""
    from ..autopilot import dealsource, ledger
    out: Counter = Counter()
    unplaced = 0
    for lead in leads:
        owner = str(ledger.fact(lead["record"], "owner_uuid") or "") or omap.get(lead["record"], "")
        who = dealsource.origin(cat, owner, lead["when"])["caller"] if owner else ""
        if who:
            out[who] += 1
        else:
            unplaced += 1
    return out, unplaced


_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _column_titles() -> dict:
    """column id -> title, from what the lead stages learned about the boards (kv), for a log row
    that names its column by id."""
    import json
    out = {}
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT value FROM kv WHERE key LIKE 'autopilot:leads:columns:%'").fetchall()
    for r in rows:
        try:
            for u, v in ((json.loads(r["value"] or "{}") or {}).get("columns") or {}).items():
                if isinstance(v, dict) and v.get("title"):
                    out[str(u).lower()] = str(v["title"])
        except (ValueError, AttributeError):
            continue
    return out


def appointments(since: datetime, until: datetime) -> dict:
    from ..autopilot import activity
    titles = None
    tasks, cards = set(), set()
    for e in activity.events("task.created", since=since, until=until):
        if str(e["data"].get("kind") or "").strip().lower() == "appointment" and e["resource"]:
            tasks.add(e["resource"])
    for e in activity.events("card.moved", since=since, until=until):
        col = str(e["data"].get("column") or "")
        if _ID.match(col):
            titles = _column_titles() if titles is None else titles
            col = titles.get(col.lower(), col)
        if "appointment" in col.lower() and e["resource"]:
            cards.add(e["resource"])
    return {"total": len(tasks | cards), "tasks": len(tasks), "cards": len(cards)}


def reach(groups, until: datetime) -> list[str]:
    """One line for each log group whose stored copy stops before the window ends."""
    from ..autopilot import activity
    out = []
    for g in groups:
        through = activity.synced_through(g)
        if through is None:
            out.append("The %s log copy has never been read on this box, so its numbers here are zero." % g)
        elif through < until:
            out.append("The %s log copy reads through %s, so anything after that is missing." % (g, _fmt(through.date())))
    return out


# ---------------------------------------------------------------------- the run
def _n(x: float) -> str:
    from ..autopilot import kpi
    return kpi.num(round(x)) if x >= 10 or abs(x - round(x)) < 0.05 else kpi.num(round(x, 1))


def run(task: dict) -> dict:
    from ..autopilot import capture, kpi
    payload = store.payload_of(task)
    week = (payload.get("inputs") or {}).get("week") or "last"
    tz = _tz()
    now = _now()
    start, end, label, wprob = window(str(week), now, tz)
    out = {"workflow": WORKFLOW, "window": {"start": start.isoformat(), "end": end.isoformat(), "label": label},
           "problems": [wprob] if wprob else [], "notes": [], "files": [], "run_dir": ""}
    try:
        cat = kpi.load()
    except kpi.KpiError as exc:
        out["problems"] += ["KPI catalog: %s" % p for p in exc.problems[:4]]
        out["catalog"] = "broken"
        return out
    since, until = kpi.day_start(start, tz), kpi.day_start(end, tz) + timedelta(days=1)
    rows = kpi.caller_days(cat, start.isoformat(), end.isoformat(), tz)
    captured = [r for r in capture.rows(since_day=start.isoformat()) if r["day"] <= end.isoformat()]
    if not captured:
        out["notes"].append("No calling day is captured in this week: the daily capture writes each caller's day at "
                            "19:15 on weekdays, so dials, answers and talk time read zero.")
    out["notes"] += reach(("calls", "state", "board"), min(until, now))

    risk = cat.share("goal.at_risk_share")
    team = Counter()
    per: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        for k in ("dials", "answered", "conversations", "talk_s", "calls_talked", "call_backs", "texts"):
            team[k] += int(r.get(k) or 0)
        if r["caller"] in NOT_CALLERS:
            continue
        p = per[r["caller"]]
        if r.get("dials"):
            p["days"] += 1
        for k in ("dials", "answered", "conversations", "talk_s", "texts", "call_backs"):
            p[k] += int(r.get(k) or 0)
    leads = kpi.new_leads(since=since, until=until)
    credited, unplaced = credit(cat, leads, owner_map())
    correct = kpi.correct_numbers(cat, since=since, until=until)
    appts = appointments(since, until)

    callers = []
    for who, p in sorted(per.items(), key=lambda kv: -kv[1]["dials"]):
        days = max(1, p["days"])
        d, c, o = p["dials"] / days, p["conversations"] / float(days), credited.get(who, 0) / float(days)
        callers.append({
            "caller": who, "calling_days": p["days"], "dials": p["dials"], "answered": p["answered"],
            "conversations": p["conversations"], "talk_min": round(p["talk_s"] / 60.0), "leads": credited.get(who, 0),
            "dials_per_day": round(d, 1), "conversations_per_day": round(c, 1), "leads_per_day": round(o, 2),
            "marks": {"dials": kpi.mark(d, cat.value("caller.dials_per_day"), risk),
                      "conversations": kpi.mark(c, cat.low("caller.conversations_per_day"), risk),
                      "leads": kpi.mark(o, cat.low("caller.opportunities_per_day"), risk)}})
    funnel = {}
    if team["dials"] and correct["total"]:
        ratio = team["dials"] / float(correct["total"])
        funnel["dials_per_correct_number"] = {"value": round(ratio, 1), "target": cat.text("funnel.dials_per_correct_number"),
                                              "mark": kpi.mark(ratio, cat.value("funnel.dials_per_correct_number"), risk, lower_is_better=True)}
    if team["dials"] and leads:
        ratio = team["dials"] / float(len(leads))
        funnel["dials_per_lead"] = {"value": round(ratio, 1), "target": cat.text("funnel.dials_per_lead"),
                                    "mark": kpi.mark(ratio, cat.high("funnel.dials_per_lead"), risk, lower_is_better=True)}
    if unplaced and leads:
        out["notes"].append("%d of %d lead(s) are credited to no caller: the log holds no conversation of theirs before the "
                            "lead, or the owner behind the record is not known on this box yet." % (unplaced, len(leads)))
    figs = [cat.fig(k) for k in USES]
    unconfirmed = sorted({f.decision for f in figs if f.status in ("recommended", "placeholder") and f.decision})
    out.update(team=dict(team), callers=callers, leads=len(leads), leads_credited=dict(credited), leads_unplaced=unplaced,
               correct=correct, appointments=appts, funnel=funnel, footnote=cat.footnote(USES),
               unconfirmed=any(f.status in ("recommended", "placeholder") for f in figs), decisions=unconfirmed,
               weekdays=kpi.weekdays(start, end + timedelta(days=1)))
    files = _write(task, out, rows, cat)
    out["files"] = [files.name]
    out["run_dir"] = str(files.parent.parent)
    return out


def _write(task: dict, r: dict, rows: list[dict], cat) -> Path:
    from ..autopilot import kpi
    d = Path(config.RUNS_DIR) / ("%s-%d" % (WORKFLOW, int(task.get("id") or 0))) / "files"
    d.mkdir(parents=True, exist_ok=True)
    t = r["team"]
    callers = r["callers"]
    cdays = sum(c["calling_days"] for c in callers)
    ans = ("%d%% of dials" % round(100.0 * t["answered"] / t["dials"])) if t.get("dials") else ""
    f = r.get("funnel") or {}
    summary = [
        ["Dials", t.get("dials", 0), "%s per caller (%s across %d calling day(s))" % (
            cat.text("caller.dials_per_day"), kpi.num(cat.value("caller.dials_per_day") * cdays), cdays), "",
         "Outbound calls in the account log, whichever dialer made them"],
        ["Answered (connects)", t.get("answered", 0), "normal is %s" % cat.text("alarm.answer_rate_normal"), ans,
         "What a dialer calls a connect. A voicemail greeting answers too, so it is not judged"],
        ["Conversations", t.get("conversations", 0), "%s per caller" % cat.text("caller.conversations_per_day"), "",
         "Answered calls of a minute or more, inbound calls that long, inbound texts"],
        ["Correct numbers", r["correct"]["total"], "%s each" % cat.text("funnel.dials_per_correct_number"),
         (f.get("dials_per_correct_number") or {}).get("mark", ""),
         "%d confirmed on a call, %d by a text reply or a person" % (r["correct"]["by_call"], r["correct"]["other"])],
        ["Leads", r["leads"], "%s per caller" % cat.text("caller.opportunities_per_day"),
         (f.get("dials_per_lead") or {}).get("mark", ""),
         "Records entering a lead status; %d credited to a caller" % sum(r["leads_credited"].values())],
        ["Appointments", r["appointments"]["total"], "no figure in the catalog", "",
         "%d appointment task(s), %d card(s) moved to Appointment Booked (that column also takes offer cards until plan 2.10)"
         % (r["appointments"]["tasks"], r["appointments"]["cards"])],
        ["Dials per correct number", (f.get("dials_per_correct_number") or {}).get("value", ""),
         cat.text("funnel.dials_per_correct_number"), (f.get("dials_per_correct_number") or {}).get("mark", ""), "Lower is better"],
        ["Dials per lead", (f.get("dials_per_lead") or {}).get("value", ""), cat.text("funnel.dials_per_lead"),
         (f.get("dials_per_lead") or {}).get("mark", ""), "Lower is better"],
    ]
    crows = [[c["caller"], c["calling_days"], c["dials"], c["dials_per_day"], cat.text("caller.dials_per_day"), c["marks"]["dials"],
              c["answered"], ("%d%%" % round(100.0 * c["answered"] / c["dials"])) if c["dials"] else "", c["conversations"],
              c["conversations_per_day"], cat.text("caller.conversations_per_day"), c["marks"]["conversations"], c["talk_min"],
              c["leads"], c["leads_per_day"], cat.text("caller.opportunities_per_day"), c["marks"]["leads"]] for c in callers]
    drows = [[x["day"], x["caller"], x["dials"], x["answered"], x["conversations"], x.get("calls_talked", 0),
              x.get("call_backs", 0), x.get("texts", 0), round((x.get("talk_s") or 0) / 60.0), x.get("owners", 0),
              x.get("numbers", 0)] for x in rows]
    how = [["Week", "%s (%d weekday(s))" % (r["window"]["label"], r["weekdays"])]]
    for name in ("conversation", "contact", "opportunity", "correct_number"):
        try:
            how.append([name.replace("_", " ").capitalize(), cat.definition(name).get("says") or ""])
        except kpi.KpiError:
            continue
    how.append(["Judged against", r["footnote"]])
    how += [["Note", n] for n in r["notes"] + r["problems"]]
    return write_workbook(d / ("kpi_%s_to_%s.xlsx" % (r["window"]["start"], r["window"]["end"])), [
        ("Summary", ["Measure", "This week", "Benchmark", "Mark", "How it is counted"], summary),
        ("Callers", ["Caller", "Calling days", "Dials", "Dials a day", "Floor", "Mark", "Answered", "Answer rate",
                     "Conversations", "Conversations a day", "Floor", "Mark", "Talk minutes", "Leads credited",
                     "Leads a day", "Floor", "Mark"], crows),
        ("Days", ["Day", "Caller", "Dials", "Answered", "Conversations", "Calls of a minute", "Call backs", "Texts in",
                  "Talk minutes", "Owners dialed", "Numbers dialed"], drows),
        ("How it is counted", ["What", "Detail"], how)])


# ---------------------------------------------------------------------- the channel
def render(task: dict, result: dict) -> str:
    w = (result.get("window") or {}).get("label") or "the week"
    if result.get("catalog") == "broken":
        return ":bar_chart: *KPIs, %s*: not built, the KPI catalog (team/kpi.yaml) has a problem.\n:small_orange_diamond: %s" % (
            w, (result.get("problems") or ["see the log"])[0])
    t = result.get("team") or {}
    a = result.get("appointments") or {}
    head = ":bar_chart: *KPIs, %s*: %s dials, %s answered, %s conversations, %s correct number(s), %s lead(s), %s appointment(s)." % (
        w, _n(t.get("dials", 0)), _n(t.get("answered", 0)), _n(t.get("conversations", 0)),
        _n((result.get("correct") or {}).get("total", 0)), _n(result.get("leads", 0)), _n(a.get("total", 0)))
    lines = [head]
    callers = result.get("callers") or []
    for c in callers[:2]:
        m = c["marks"]
        lines.append("%s, %d calling day(s): %s dials a day (%s), %s conversations a day (%s), %s leads a day (%s)." % (
            c["caller"], c["calling_days"], _n(c["dials_per_day"]), m["dials"], _n(c["conversations_per_day"]),
            m["conversations"], _n(c["leads_per_day"]), m["leads"]))
    if len(callers) > 2:
        lines.append("And %d more caller(s) in the workbook." % (len(callers) - 2))
    if not t.get("dials"):
        lines.append(((result.get("notes") or []) + ["No dials are in the log copy for that week."])[0])
    url = ""
    for name in result.get("files") or []:
        url = slack.file_url(task.get("id") or 0, name) or url
    tail = []
    if url:
        tail.append("Workbook: %s." % url)
    elif result.get("files"):
        tail.append("The workbook is on the brain under %s (no public link: AGENTS_PUBLIC_URL is not set)." % (
            Path(result.get("run_dir") or "").name or "its run folder"))
    if result.get("unconfirmed"):
        tail.append("The floors are recommended, not yet confirmed%s." % (
            " (%s)" % ", ".join(result.get("decisions") or []) if result.get("decisions") else ""))
    if tail:
        lines.append(" ".join(tail))
    probs = result.get("problems") or []
    if probs:
        lines.append(":small_orange_diamond: %s" % probs[0])
    return "\n".join(lines)
