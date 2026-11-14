"""Alarms (build plan 6.3): rules over the daily capture, each said once, each naming what to check.

A caller can make 500 dials without a lead and nobody notices for two weeks, because each single day
only looks slow. The teaching gives five signs that something is broken, not slow, and for each one
the FIRST thing to look at, which is rarely the caller:

    no lead        500 dials since the last new lead: the caller ID spam flags first, then the list
    padded dials   many dials and almost no talk time: dials that end at once; listen to a few
    bad list       a day's dials 20% above the caller's own average: fewer people are picking up
    answer rate    under 50% of dials answered (70% is ordinary): a trace or a spam fault
    first trace    the first skip trace found a number on under 90% of new records: the trace run

Every threshold is a figure in the KPI catalog (team/kpi.yaml); this module holds none, and each
alarm says which window its number covers.

ONCE PER CAUSE PER DAY. An alarm is kept under the captured day it judges, the rule, and who it is
about. The first run that sees it posts it; a later run for the same day lists it as a plain line
and posts nothing, so a rerun cannot say it twice. A cause that is still true tomorrow is raised
tomorrow, once: a standing fault is repeated daily, never hourly.

An alarm has three parts, kept apart so each reader gets what it needs: WHAT happened (the number,
its window, the threshold), what to CHECK first, and the list to move to. The evening post carries
all three in one line. The morning digest quotes only the first, because a digest is a glance.

The list to move to is the first list in the playbook's own ladder order (`ready_lists`, first to
market first) that has records ready and is not the one the dials were already on. The count behind
it is read only when a new alarm fires.

When two callers dial on one day the log cannot always tell whose dial is whose (a dial carries no
name, the answer does), so that day is judged for all callers together and says so.

It reads only. Nothing here changes the account.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .. import store
from . import activity, capture, kpi, lanes, pacing, windows
from .stages import Ctx, merge_must

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_alarms (
    day TEXT, code TEXT, subject TEXT, what TEXT, text TEXT, win TEXT, at TEXT,
    PRIMARY KEY (day, code, subject));
"""
ACCOUNT = "the account"
EVERYONE = "all callers"
ORDER = ("no_lead", "padded_dials", "answer_rate", "dial_jump", "first_trace")     # most serious first
SHOWN = 3              # how many problem lines of one run the channel shows (sweep.slack_text)
NOT_CALLERS = ("unknown", kpi.UNASSIGNED)
USES = ("alarm.dials_without_lead", "alarm.min_talk_seconds_per_dial", "alarm.min_dials_to_judge", "alarm.dial_jump",
        "alarm.dial_jump_baseline_days", "alarm.answer_rate_min", "alarm.answer_rate_normal",
        "alarm.first_trace_hit_min", "alarm.first_trace_window_days", "alarm.first_trace_min_records",
        "alarm.flagged_lines_normal")


@dataclass
class Alarm:
    code: str
    subject: str
    what: str          # what happened: the number, its window and the threshold, with no full stop
    check: str         # what to look at first, one sentence
    win: str           # the window the number covers, in words

    def text(self, move_to: str = "") -> str:
        """The whole alarm in one plain line: what happened, what to check first, the list to move to."""
        return ("%s. %s %s" % (self.what, self.check, move_to)).strip()


# ---------------------------------------------------------------------- the memory: one row per cause per day
def raise_once(day: str, alarm: Alarm, text: str, now: datetime) -> bool:
    """True the first time this cause is seen for this captured day, False ever after."""
    kpi.ensure("ap_alarms", SCHEMA)
    with store._LOCK, store._db() as c:
        cur = c.execute("INSERT OR IGNORE INTO ap_alarms (day, code, subject, what, text, win, at) VALUES (?,?,?,?,?,?,?)",
                        (day, alarm.code, alarm.subject, alarm.what, text, alarm.win,
                         now.astimezone(timezone.utc).isoformat(timespec="seconds")))
        return bool(cur.rowcount)


def raised(day: str = "", since_day: str = "") -> list[dict]:
    """The alarms on record, by day and then most serious first: for one captured day, or from a day on."""
    kpi.ensure("ap_alarms", SCHEMA)
    q, args = "SELECT * FROM ap_alarms WHERE 1=1", []
    if day:
        q += " AND day=?"
        args.append(day)
    if since_day:
        q += " AND day>=?"
        args.append(since_day)
    with store._LOCK, store._db() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    return sorted(rows, key=lambda r: (r["day"], ORDER.index(r["code"]) if r["code"] in ORDER else len(ORDER), r["subject"]))


def digest_lines(now: datetime | None = None, tz: str = "America/New_York", limit: int = 3, max_age_days: int = 4) -> list[str]:
    """Ready lines for the morning digest: what happened in each alarm of the last day that had any,
    at most `limit`, without the advice (that went out with the alarm itself the evening before). An
    alarm older than `max_age_days` is left out: four days carries a Friday alarm to Monday's digest
    and no further."""
    now = now or datetime.now(timezone.utc)
    today = date.fromisoformat(kpi.local_day(now, tz))
    rows = raised(since_day=(today - timedelta(days=max_age_days)).isoformat())
    if not rows:
        return []
    last = max(r["day"] for r in rows)
    return ["Alarm %s: %s." % (kpi.fmt_day(last), r["what"]) for r in rows if r["day"] == last][:limit]


# ---------------------------------------------------------------------- who and what is judged
def judged(rows: list[dict]) -> list[dict]:
    """The rows a rate can be worked out for. When some dials carry no caller's name, the callers
    are judged together: a named row would otherwise show answers with no dials behind them."""
    unnamed = sum(r["dials"] for r in rows if r["caller"] == "unknown")
    named = [r for r in rows if r["caller"] not in NOT_CALLERS]
    if not unnamed:
        return named
    all_rows = [r for r in rows if r["caller"] != kpi.UNASSIGNED]
    return [{"day": all_rows[0]["day"], "caller": EVERYONE, "dials": sum(r["dials"] for r in all_rows),
             "answered": sum(r["answered"] for r in all_rows), "talk_s": sum(r["talk_s"] for r in all_rows)}]


def history(day: str, back_days: int = 75) -> dict:
    """caller -> [(day, dials)] for the calling days before `day`, oldest first, with one more entry
    for all callers together."""
    since = (date.fromisoformat(day) - timedelta(days=back_days)).isoformat()
    out: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    for r in capture.rows(since_day=since):
        if r["day"] >= day or not r["dials"]:
            continue
        out[EVERYONE][r["day"]] += r["dials"]
        if r["caller"] not in NOT_CALLERS:
            out[r["caller"]][r["day"]] += r["dials"]
    return {who: sorted(days.items()) for who, days in out.items()}


# ---------------------------------------------------------------------- the five rules
def no_lead(cat: kpi.Catalog, day_end: datetime, tz: str) -> Alarm | None:
    """500 dials with no lead. Counted from the last new lead, or from the start of the log when the
    log holds no lead at all (then the alarm says so: the count is a floor, not the whole story)."""
    limit = cat.value("alarm.dials_without_lead")
    leads = kpi.new_leads(until=day_end)
    if leads:
        since = leads[-1]["when"]
        n = kpi.dials(since=since, until=day_end)
        what = "%s dials since the last new lead on %s" % (kpi.num(n), kpi.fmt_day(kpi.local_day(since, tz)))
        win = "since the last new lead, %s" % kpi.fmt_day(kpi.local_day(since, tz))
    else:
        reach = kpi.log_reach(activity.DIAL)
        if reach is None:
            return None
        n = kpi.dials(since=reach, until=day_end)
        what = "%s dials since %s, as far back as the call log reaches, and no new lead at all" % (
            kpi.num(n), kpi.fmt_day(kpi.local_day(reach, tz)))
        win = "the whole call log, from %s" % kpi.fmt_day(kpi.local_day(reach, tz))
    if n < limit:
        return None
    return Alarm("no_lead", ACCOUNT, "%s (the alarm is %s)" % (what, kpi.num(limit)),
                 "Check the caller ID spam flags first (about %s%% of lines flagged is an ordinary day), then the list." % kpi.num(
                     cat.value("alarm.flagged_lines_normal")), win)


def padded(cat: kpi.Catalog, r: dict) -> Alarm | None:
    """Talk time low for the dial count: a caller who dials and ends the call at once shows many
    dials and almost no talk."""
    if r["dials"] < cat.value("alarm.min_dials_to_judge"):
        return None
    per = r["talk_s"] / float(r["dials"])
    floor = cat.value("alarm.min_talk_seconds_per_dial")
    if per >= floor:
        return None
    return Alarm("padded_dials", r["caller"], "%s: %s dials and %d min of talk on %s, %s seconds a dial (the alarm is under %s)" % (
        r["caller"], kpi.num(r["dials"]), round(r["talk_s"] / 60.0), kpi.fmt_day(r["day"]), kpi.num(round(per, 1)), kpi.num(floor)),
        "Dials that end at once look like this: listen to five of those calls first.", "the one day, %s" % kpi.fmt_day(r["day"]))


def dial_jump(cat: kpi.Catalog, r: dict, before: list) -> tuple[Alarm | None, str]:
    """Dials up 20%: more dials in the same hours means fewer people picking up, which is a bad list.
    Judged against the caller's own earlier calling days; with too few of those it is not judged, and
    the second value says so."""
    need = int(cat.value("alarm.dial_jump_baseline_days"))
    if r["dials"] < cat.value("alarm.min_dials_to_judge"):
        return None, ""
    base = [n for _, n in before][-need:]
    if len(base) < need:
        return None, "the dial jump was not judged: fewer than %d earlier calling days are captured" % need
    usual = sum(base) / float(len(base))
    jump = cat.value("alarm.dial_jump")
    if usual <= 0 or r["dials"] < usual * (1 + jump / 100.0):
        return None, ""
    over = round((r["dials"] / usual - 1) * 100)
    return Alarm("dial_jump", r["caller"], "%s: %s dials on %s, %d%% above the usual %s (the alarm is %s%% above the average of the "
                 "%d calling days before)" % (r["caller"], kpi.num(r["dials"]), kpi.fmt_day(r["day"]), over, kpi.num(round(usual)),
                                             kpi.num(jump), need),
                 "More dials in the same hours means fewer people picking up: check the list first.",
                 "%s against the %d calling days before" % (kpi.fmt_day(r["day"]), need)), ""


def answer_rate(cat: kpi.Catalog, r: dict) -> Alarm | None:
    """Answer rate under 50%. An answer is not a conversation (a voicemail greeting answers), which
    is why a low answer rate points at the numbers and the lines, not at the caller."""
    if r["dials"] < cat.value("alarm.min_dials_to_judge"):
        return None
    rate = 100.0 * r["answered"] / float(r["dials"])
    floor = cat.value("alarm.answer_rate_min")
    if rate >= floor:
        return None
    return Alarm("answer_rate", r["caller"], "%s: %d%% of %s dials answered on %s (the alarm is under %s%%, and about %s%% is ordinary)" % (
        r["caller"], round(rate), kpi.num(r["dials"]), kpi.fmt_day(r["day"]), kpi.num(floor), kpi.num(cat.value("alarm.answer_rate_normal"))),
        "That is a trace or a spam fault: check the caller ID spam flags first, then how old the skip trace on those numbers is.",
        "the one day, %s" % kpi.fmt_day(r["day"]))


def first_trace(ctx: Ctx, cat: kpi.Catalog) -> tuple[Alarm | None, str]:
    """A first trace that finds numbers on under 90%.

    Read from the account's own presets, so no record is opened: of the records added in the window
    (the playbook's `trace_check.scope_tags`), those still waiting for a trace are set aside, and the
    rest are split by whether they sit in the traced-with-no-number preset. That makes the rate an
    upper bound: a record a later source rescued, or one a preset exclusion hides, reads as found. An
    alarm under the floor is therefore real, and a pass is not proof."""
    cfg = ctx.stage.get("trace_check") or {}
    tags, waiting_t, none_t = cfg.get("scope_tags") or [], cfg.get("waiting_preset"), cfg.get("no_number_preset")
    if not (tags and none_t):
        return None, "the trace rate was not judged: trace_check is not set in the playbook"
    days = int(cat.value("alarm.first_trace_window_days"))
    today = date.fromisoformat(kpi.local_day(ctx.now, ctx.policy.tz))
    created = {"created": [(today - timedelta(days=days)).isoformat(), (today + timedelta(days=1)).isoformat()]}
    ids = [ctx.crm.tag_uuid(t) for t in tags]
    if not all(ids):
        return None, "the trace rate was not judged: a tag in trace_check.scope_tags does not exist in this account"

    def count_preset(title: str) -> int:
        must, _ = windows.resolve(ctx.crm.preset_must(title), ctx.now)
        return ctx.crm.count(merge_must(must, created))

    try:
        total = ctx.crm.count({"all_tags": ids, **created})
        waiting = count_preset(waiting_t) if waiting_t else 0
        none = count_preset(none_t)
    except KeyError as exc:
        return None, "the trace rate was not judged: %s" % exc
    traced = total - waiting
    if traced < cat.value("alarm.first_trace_min_records"):
        return None, "the trace rate was not judged: only %d traced record(s) in the last %d days" % (max(traced, 0), days)
    rate = 100.0 * (traced - none) / float(traced)
    floor = cat.value("alarm.first_trace_hit_min")
    if rate >= floor:
        return None, ""
    label = str(cfg.get("label") or "new records")
    return Alarm("first_trace", ACCOUNT, "The first skip trace found a number on at most %d%% of the %s %s added in the last %d days "
                 "(the alarm is under %s%%)" % (round(rate), kpi.num(traced), label, days, kpi.num(floor)),
                 "Check the trace run first: which records it covered and which source ran.",
                 "records added in the last %d days" % days), ""


# ---------------------------------------------------------------------- the list to move to
def working_lane(ctx: Ctx, caller: str, start: datetime, end: datetime) -> str:
    """The lane most of the day's answered calls were on, from the nightly owner-lane map (no reads).
    "" when it cannot be told."""
    if not pacing.lane_map_built(ctx.policy):
        return ""
    seen: Counter = Counter()
    for e in activity.events("owner.call.answered", since=start, until=end):
        d = e["data"]
        if d.get("direction") == "inbound" or not e["resource"]:
            continue
        if caller not in (EVERYONE, ACCOUNT) and (d.get("caller") or "").strip() != caller:
            continue
        held = lanes.owner_lane(e["resource"])
        seen[pacing.FOLD.get(held["lane"], held["lane"]) if held else lanes.DEFAULT_LANE] += 1
    return seen.most_common(1)[0][0] if seen else ""


def next_list(ctx: Ctx, skip_lane: str = "") -> str:
    """The first list in the playbook's ladder order with records ready, other than the lane the
    dials were already on. One count per list tried, stopping at the first that has records."""
    for item in ctx.stage.get("ready_lists") or []:
        if not isinstance(item, dict) or not item.get("preset"):
            continue
        if skip_lane and str(item.get("lane") or "") == skip_lane:
            continue
        title = str(item["preset"])
        try:
            must, _ = windows.resolve(ctx.crm.preset_must(title), ctx.now)
            n = ctx.crm.count(must)
        except KeyError as exc:
            ctx.out.problem("%s (it cannot be suggested as the next list)" % exc)
            continue
        if n > 0:
            return "Next list ready: %s (%s)." % (title, kpi.num(n))
    return ""


# ---------------------------------------------------------------------- the stage
def run_alarms(ctx: Ctx) -> None:
    st = ctx.stage
    cat = kpi.for_stage(ctx)
    if cat is None:
        return
    tz = ctx.policy.tz
    seconds = int(cat.definition("conversation")["min_seconds"])
    for sid, other in ctx.policy.stages.items():
        if other.kind == "capture" and int(other.get("conversation_seconds", 60)) != seconds:
            ctx.out.problem("the daily capture (%s) counts a conversation at %d seconds and the catalog says %d: one "
                            "of them is wrong" % (sid, int(other.get("conversation_seconds", 60)), seconds))
    kpi.sync_log(ctx, ("calls", "state"))
    captured = capture.rows()
    if not captured:
        ctx.out.problem("the daily capture has written no day yet, so there is nothing to judge")
        ctx.out.head = "No alarm was judged: nothing is captured yet."
        return
    day = max(r["day"] for r in captured)
    age = (date.fromisoformat(kpi.local_day(ctx.now, tz)) - date.fromisoformat(day)).days
    if age > int(st.get("stale_days", 4)):
        ctx.out.problem("the last captured day is %s: the daily capture has not run since, so no alarm was judged" % kpi.fmt_day(day))
        ctx.out.head = "No alarm was judged: the capture is %d days old." % age
        return
    start = kpi.day_start(day, tz)
    end = start + timedelta(days=1)
    rows = judged(kpi.caller_days(cat, day, day, tz))
    before = history(day)
    known = {(r["code"], r["subject"]): r["text"] for r in raised(day=day)}
    ctx.out.found = len(rows)

    found: list[Alarm] = []
    a = no_lead(cat, end, tz)
    if a:
        found.append(a)
    for r in rows:
        for a in (padded(cat, r), answer_rate(cat, r)):
            if a:
                found.append(a)
        a, note = dial_jump(cat, r, before.get(r["caller"], []))
        if a:
            found.append(a)
        elif note:
            ctx.out.tally[note] += 1
    if ("first_trace", ACCOUNT) in known:                 # raised already: its counts are not read again
        found.append(Alarm("first_trace", ACCOUNT, "", "", ""))
    else:
        a, note = first_trace(ctx, cat)
        if a:
            found.append(a)
        elif note:
            ctx.out.tally[note] += 1

    new = 0
    move_to: dict[str, str] = {}
    for a in found:
        if (a.code, a.subject) in known:
            ctx.out.line("Already raised: %s" % known[(a.code, a.subject)])
            continue
        lane = working_lane(ctx, a.subject, start, end)
        if lane not in move_to:
            move_to[lane] = next_list(ctx, skip_lane=lane)
        text = a.text(move_to[lane])
        if raise_once(day, a, text, ctx.now):
            new += 1
            # The channel shows the first three problems of a run. A fourth alarm on the same evening
            # goes out as a line as well, so no alarm is ever the one that was cut.
            if len(ctx.out.problems) >= SHOWN:
                ctx.out.line("Also: %s" % text)
            ctx.out.problem(text)
            ctx.out.tally["alarm: %s" % a.code.replace("_", " ")] += 1
    ctx.out.acted = new
    earlier = len(found) - new
    if new:
        ctx.out.line(cat.footnote(USES))
        ctx.out.head = "%s: %d alarm(s) on the day's numbers%s." % (
            kpi.fmt_day(day), new, (", and %d already raised" % earlier) if earlier else "")
    elif earlier:
        ctx.out.head = "%s: nothing new. %d alarm(s) were already raised for that day." % (kpi.fmt_day(day), earlier)
    else:
        ctx.out.head = "%s: no alarm. %d caller day(s) checked against the catalog." % (kpi.fmt_day(day), len(rows))
