"""The First to Market board (the owner's ask, 2026-11-14): every county record on one SiftLine
board, worked through five full attempts on consecutive business days.

    Research   a probate or obituary record waits here until research names a decision maker with
               a number a caller may dial (Dial First or Second); the research queue takes it
    Day 1..5   one full attempt a business day: that day's call and that day's text (the call alone
               when the owner has no mobile a text may go to, or while the county texts are not live)
    Exhausted  five full attempts and no contact: the follow-up texts and the monthly mail begin

WHY A BOARD. The county records (foreclosures, probates, obituaries, liens, everything the county
publishes) shared the Priority presets with the Top 1,000, and their round was a call counter
nobody could see. The team works a board, so the round lives on one.

THREE PIPELINES, EVERY RECORD IN ONE. County: this board (tag FTM, the Obituary list). The Top
1,000: its own presets, the flow it always had, no county record in it. The agency: sent once a
month, nothing in-house. Heirs and hot auction keep their own rules above the board: a sale inside
the hot-auction window moves on its call alone, the way that lane always counted its round, and
its lane keeps calling after Day 5.

ONE OWNER FOR EACH DECISION.
    the column a card is in        this stage; nothing else moves a card it placed
    whether a call happened        the shared call log, by the attempt keeper's rule: a business
                                   day with an outbound dial (inside calling hours)
    whether a text happened        the text ledger: a text handed over to the owner, by any stage,
                                   or one the record's own counter shows from the other system
    the call counter               the attempt keeper, which on a live board writes the number of
                                   full attempts the board counted, so every preset keyed on the
                                   counter shows what the board shows
    which text goes today          the text lane, told here which text the card's column stands for
    when mail and follow-ups open  the lane timeline, asked with the board's day
    who researches                 the research queue; this stage says who waits on it and why

A DAY WITHOUT THE ATTEMPT HOLDS THE CARD. A card moves only in a pass that runs before calls start,
judged on the business days that are over, so it can never gain a column for a day still in
progress, never moves twice in one business day, and each business day earns at most one column.
A text sent on a held day still counts once the call comes: the owner is not texted twice for one
column. The late pass places new records, takes a researched record out of Research and says,
while there is still time, which cards are short of today's call or text.

A LEAD LEAVES. Any lead status, on this record or on another of the owner's, takes the card off the
board: the lead board owns the owner now. A sold house, Do Not Market, an opt-out, a litigator or a
finished status take it off too.

PLAN MODE simulates the board in the autopilot's own memory, under a fact no other stage reads, so a
plan run on the second morning shows the moves the live board would make. Nothing reaches DataSift
from a plan. A live card create, move and removal goes through ctx.write, with the value it
replaced recorded first, so `autopilot undo <run>` puts a run back (leadroutes.undo_step).

Every route here is in the published reference: the board listing, the card list, create, read,
move and delete, and a card's timeline. The board's columns cannot be listed on the key, so the
playbook names them (column_ids), written out by the account tool (pipelines.py) after it makes the
board.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from . import activity, gate, lanes, ledger, leadroutes, limits, scrub
from .stages import Ctx, addr, marks_of, preset_rows, status_of

log = logging.getLogger(__name__)

KIND = "ftmpipeline"
BOARD = "First to Market"
RESEARCH, EXHAUSTED = "Research", "Exhausted"
DAYS = 5
DAY_COLUMNS = tuple("Day %d" % n for n in range(1, DAYS + 1))
COLUMNS = (RESEARCH,) + DAY_COLUMNS + (EXHAUSTED,)
# The lanes whose records live on the board: the two county lanes, and the two above them a county
# record can also sit in (a sale inside the window, a verified heir). The Top 1,000 and the agency
# are never on it.
BOARD_LANES = ("heirs", "hot_auction", "ftm", "obituary")
FACT = "ftm_pipeline"            # the record's card as the live board holds it
PLAN_FACT = "ftm_pipeline_plan"  # where a plan run would have put it; read by this stage's plan runs only
OFF_FACT = "ftm_pipeline_off"    # a card a person took off the board: never put back on its own
STATE_EVENTS = ("property.status.updated", "property.tags.added", "property.tags.removed", "property.lists.added")
# Gate findings that take a card off the board for good: nothing in the round may touch the record.
GONE_CODES = ("do_not_market", "litigator", "opted_out", "sold", "finished", "listed", "not_single_family")
REQUIRE = ("auto", "always", "never")
BACKLOG = ("counter", "day1")
DONE_LEAD = "a lead: the lead board has it"

DEFAULTS = {
    "board": BOARD,                      # the SiftLine board's title, as the lead stages name theirs
    "column_ids": {},                    # column title -> uuid, written out by the account tool's apply
    "obituary_presets": ["Obituary - All"],   # the second door: a preset cannot say "tag FTM or list Obituary"
    "research_first": ["Probate", "Obituary"],  # lists or tags whose records wait in Research for a decision maker
    "researched_tags": ["Deep Prospected", "Heir Identified", "Heirs Resolved"],
    "dm_fields": ["Decision Maker"],     # a custom field research fills when it names who signs
    "off_board_tags": ["Wrong House"],   # research found the house is not the one to market
    "text_days": [1, 2, 3],              # the days whose full attempt includes a text (the opening texts)
    "require_text": "auto",              # auto: only while text_stage is live on a live box; always; never
    "text_stage": "ftm_texts",
    "call_alone_lanes": ["hot_auction"],  # foreclosure urgency: the round moves on the call alone, as that lane always counted it
    "lead_column": "",                   # empty: a lead's card leaves the board; a column title moves it there instead
    "backlog": "counter",                # counter: a record called before the board went live starts where its counter says
    "late_after": "12:00",               # a pass that starts after this is the late pass (it never moves a day)
    "night_before": "06:00",             # a pass before this has the night's larger read budget
    "floor_days": 45,
    "max_log_calls": 400,
    "max_reads": 200,                    # full records opened a run
    "night_reads": 400,
    "max_creates": 200,
    "max_moves": 300,
    "max_cards": 1000,                   # cards listed per column
    "max_scan": 3000,                    # rows read per preset
    "recheck_hours": 20,                 # how long what a record read said is trusted, when no event says it changed
    "off_days": 30,                      # a card a person took off stays off this long
}


def _list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _hm(text) -> int | None:
    try:
        h, m = (int(x) for x in str(text).strip().split(":"))
    except ValueError:
        return None
    return h * 60 + m if 0 <= h < 24 and 0 <= m < 60 else None


def options(st) -> dict:
    """The stage block over the defaults above."""
    o = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k)
        if v is not None:
            o[k] = v
    o["column_ids"] = {str(k): str(v) for k, v in dict(o.get("column_ids") or {}).items() if v}
    for k in ("obituary_presets", "research_first", "researched_tags", "dm_fields", "off_board_tags", "call_alone_lanes"):
        o[k] = [str(x) for x in _list(o[k]) if str(x).strip()]
    o["text_days"] = [x for x in _list(o["text_days"]) if isinstance(x, int) and not isinstance(x, bool)]
    return o


def block_problems(st) -> list[str]:
    """What is wrong with a board stage's playbook block, in the playbook checker's words, so a typo
    fails `cli.py autopilot check` instead of reading as a default on the first live morning."""
    out = []
    if not str(st.get("board") or BOARD).strip():
        out.append("stage %s: board must name the SiftLine board" % st.id)
    if st.get("pipeline") is not None:
        out.append("stage %s: `pipeline` is how another stage follows this board; the board stage itself takes none" % st.id)
    ids = st.get("column_ids")
    if ids is not None:
        if not isinstance(ids, dict):
            out.append("stage %s: column_ids must map a column title to its uuid" % st.id)
        else:
            bad = sorted(str(k) for k in ids if str(k) not in COLUMNS)
            if bad:
                out.append("stage %s: column_ids names %s; the board's columns are %s" % (st.id, bad, ", ".join(COLUMNS)))
    td = st.get("text_days")
    if td is not None and (not isinstance(td, list) or any(isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= DAYS
                                                           for x in td)):
        out.append("stage %s: text_days lists the days of the round (1 to %d) whose attempt includes a text" % (st.id, DAYS))
    rq = st.get("require_text")
    if rq is not None and str(rq) not in REQUIRE:
        out.append("stage %s: require_text must be %s" % (st.id, ", ".join(REQUIRE)))
    ca = st.get("call_alone_lanes")
    if ca is not None and (not isinstance(ca, list) or [x for x in ca if str(x) not in lanes.ORDER]):
        out.append("stage %s: call_alone_lanes must list lanes (%s)" % (st.id, ", ".join(lanes.ORDER)))
    bl = st.get("backlog")
    if bl is not None and str(bl) not in BACKLOG:
        out.append("stage %s: backlog must be %s" % (st.id, " or ".join(BACKLOG)))
    for key in ("late_after", "night_before"):
        v = st.get(key)
        if v is not None and _hm(v) is None:
            out.append("stage %s: %s must read like 12:00" % (st.id, key))
    lc = st.get("lead_column")
    if lc and str(lc) not in COLUMNS:
        out.append("stage %s: lead_column must be empty (the card leaves the board) or one of %s" % (st.id, ", ".join(COLUMNS)))
    for key in ("max_reads", "night_reads", "max_creates", "max_moves", "max_cards", "max_scan", "floor_days",
                "max_log_calls", "recheck_hours", "off_days"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a whole number, 0 or more" % (st.id, key))
    ts = st.get("text_stage")
    if ts is not None and not isinstance(ts, str):
        out.append("stage %s: text_stage names the stage that sends the county texts" % st.id)
    return out


# ---------------------------------------------------------------------- columns and days
def column_no(title: str) -> int:
    """Research 0, Day 1 to Day 5 as 1 to 5, Exhausted 6, anything else -1."""
    t = str(title or "").strip()
    if t == RESEARCH:
        return 0
    if t == EXHAUSTED:
        return DAYS + 1
    if t in DAY_COLUMNS:
        return int(t.split()[1])
    return -1


def column_title(n: int) -> str:
    return RESEARCH if n <= 0 else EXHAUSTED if n > DAYS else "Day %d" % n


def next_business_day(d: date, days) -> date:
    d += timedelta(days=1)
    while d.weekday() not in days:
        d += timedelta(days=1)
    return d


def back_business_days(d: date, n: int, days) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() in days:
            n -= 1
    return d


def due_day(entered: datetime, zone, days, first_minute: int) -> date:
    """The business day a column's attempt is first due: the day the card entered the column when
    that was a business day before calls start, else the next business day. A card placed at 8:35
    is due that day; one moved at 5pm is due tomorrow, so a call made after a move never counts for
    the new column."""
    local = entered.astimezone(zone)
    d = local.date()
    if d.weekday() in days and local.hour * 60 + local.minute < first_minute:
        return d
    return next_business_day(d, days)


# ---------------------------------------------------------------------- evidence
def dial_dates(owner_uuid: str, since: datetime, zone, hours) -> set:
    """The business-local days with an outbound dial to this owner inside calling hours. The attempt
    keeper's rule (activity.dial_days), with the hours applied: a 9pm dial is one the touch audit
    flags, not one the round may count."""
    out: set = set()
    if not owner_uuid:
        return out
    for e in activity.events(activity.DIAL, resource=owner_uuid, since=since):
        if e["data"].get("direction") == "inbound":
            continue
        local = e["when"].astimezone(zone)
        m = local.hour * 60 + local.minute
        if hours[0] <= m < hours[1]:
            out.add(local.date())
    return out


def text_dates(owner_uuid: str, since_day: str) -> set:
    """The days a text to this owner was handed over: released from any stage's batch, or seen on
    the record's own counter from the other texting system. A text staged and never released was
    never sent, so it does not count."""
    out: set = set()
    if not owner_uuid:
        return out
    for r in limits.rows(owner_uuid, since_day=since_day, statuses=("released",)):
        try:
            out.add(date.fromisoformat(r["day"]))
        except (TypeError, ValueError):
            continue
    return out


def judge(n: int, due: date, until: date, calls: set, texts: set, needs_text, days) -> tuple[int, list]:
    """(the column the evidence earns, the misses as (day, what) pairs). Walks the business days from
    the column's due day through `until`. A day with a call, and with a text sent since the card
    entered the column whenever that column needs one, earns one column; the next column's text is
    counted from the next business day. Any other day is a miss and the card holds. One day can earn
    one column at most, however many calls it carried."""
    misses: list = []
    start, d = due, due
    while d <= until and 1 <= n <= DAYS:
        if d.weekday() in days:
            texted = not needs_text(n) or any(start <= t <= d for t in texts)
            if d in calls and texted:
                n += 1
                start = next_business_day(d, days)
            else:
                misses.append((d, "no call" if d not in calls else "no text"))
        d += timedelta(days=1)
    return n, misses


# ---------------------------------------------------------------------- the record
def _low(items) -> set:
    return {str(x).strip().lower() for x in items or [] if str(x).strip()}


def custom_of(ctx: Ctx, u: str, reads: list) -> dict:
    """The record's custom fields, from the gate's week-long cache or one read (counted)."""
    got = ledger.fact(u, "custom_fields", max_age=timedelta(days=1))
    if isinstance(got, dict):
        return got
    try:
        got = ctx.crm.custom_fields(u)
    except Exception as exc:  # noqa: BLE001  (a failed field read is said, and the record waits)
        ctx.out.problem("could not read a record's custom fields (%s)" % str(exc)[:100])
        return {}
    reads[0] += 1
    ledger.set_fact(u, "custom_fields", got)
    return got


def dm_named(rec: dict, custom: dict | None, o: dict) -> str:
    """Why research is taken to have named a decision maker, or '' when it has not: a researched tag
    on the record, or a decision maker field it filled."""
    hit = marks_of(rec) & _low(o["researched_tags"])
    if hit:
        return "tagged %s" % sorted(hit)[0]
    want = _low(o["dm_fields"])
    for label, value in (custom or {}).items():
        if str(label).strip().lower() in want and str(value or "").strip():
            return "the %s field names one" % label
    return ""


def numbers(ctx: Ctx, rec: dict, row: dict | None, channel: str, lane: str) -> list:
    """The numbers a call or a text may use on this record: the gate's usable numbers (Dial First or
    Second, or one a person confirmed; a mobile for a text), less any number the do-not-call registry
    flags while those are held (decision D32)."""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    keep, _ = scrub.split(gate.usable_numbers(rec, channel, cfg, row), channel, ctx.policy, row, lane, ctx.now)
    return keep


def record_state(ctx: Ctx, rec: dict, o: dict, custom: dict | None = None) -> tuple[str, str]:
    """(lead | gone | research | exhausted | work | unknown, why) for one full record.

    lead       a lead status here, or the owner is a lead on another record: the lead board has it
    gone       nothing in the round may touch it again (the gate's hard holds), or it is no longer a
               county record, or research found the wrong house
    research   a possible death with no decision maker named, the Deep Prospecting status, or no
               number left after every trace (Mail Only): research comes before any more calls
    exhausted  the record's own status says the round is over
    unknown    a status the gate does not know: the card is left where it is and the run says so"""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    s = status_of(rec)
    if s in lanes.LEAD_STATUSES:
        return "lead", "status %s" % s
    ow = lanes.owner_lane((rec.get("owner") or {}).get("uuid") or "")
    if ow and ow.get("lane") == "lead" and (ow.get("records") or {}).get(rec.get("uuid") or "") != "lead":
        return "lead", "the owner is a lead on another record"
    v = gate.check(rec, "assign", cfg=cfg, lanes_cfg=lanes_cfg, custom=custom)
    codes = {c: d for c, d in v.reasons}
    for c in GONE_CODES:
        if c in codes:
            return "gone", (gate.TEXT[c] % codes[c]) if "%s" in gate.TEXT[c] else gate.TEXT[c]
    if marks_of(rec) & _low(o["off_board_tags"]):
        return "gone", "research found it is not the house to market"
    if lanes.lane_of(rec, lanes_cfg) not in BOARD_LANES:
        return "gone", "no longer a county record"
    if "unknown_status" in codes:
        return "unknown", "status %s is not one the gate knows" % codes["unknown_status"]
    if s == "exhausted":
        return "exhausted", "status Exhausted"
    if "in_research" in codes:
        return "research", "status Deep Prospecting: research is running (clear the status when it is done)"
    if "mail_only" in codes:
        return "research", "no number left after every trace (Mail Only)"
    if "deceased" in codes and not dm_named(rec, custom, o):
        return "research", "a possible death (%s): research first" % codes["deceased"]
    return "work", ""


def placement(ctx: Ctx, rec: dict, row: dict | None, o: dict, custom: dict | None, lane: str) -> tuple[str, str]:
    """(column, why) for a county record with no card yet, or ('', why) when it does not go on the
    board. A foreclosure, a lien or any record whose owner is known starts at Day 1, even with no
    number yet (it holds there until a trace lands one). A probate or obituary record starts in
    Research until research names a decision maker with a number a caller may dial."""
    state, why = record_state(ctx, rec, o, custom)
    if state in ("lead", "gone", "unknown"):
        return "", why
    if state == "research":
        return RESEARCH, why
    if state == "exhausted":
        return EXHAUSTED, why
    first = marks_of(rec) & _low(o["research_first"])
    if first:
        kind = sorted(first)[0]
        if not dm_named(rec, custom, o):
            return RESEARCH, "%s record: research has not named a decision maker yet" % kind
        if not numbers(ctx, rec, row, "call", lane):
            return RESEARCH, "research named a decision maker, with no number a caller may dial yet"
    c = int(rec.get("predictivecall_attempts") or 0)
    if o["backlog"] == "counter" and c > 0:
        return column_title(c + 1), "already called on %d day(s) before the board" % c
    return "Day 1", "the owner is known: the round starts"


# ---------------------------------------------------------------------- what other stages ask
def board_live(pol, sid: str) -> bool:
    st = (getattr(pol, "stages", None) or {}).get(sid)
    return bool(st is not None and st.kind == KIND and st.mode == "live" and config.live())


def follows(ctx: Ctx, lane: str) -> str:
    """The id of the board stage this stage follows for a record in `lane`, when that board is live;
    '' otherwise, and then the stage keeps the lane days it always counted. A stage opts in with
    `pipeline: <stage id>` in its block (`board` already names a board's title on the lead stages).
    Until the board is live there is no board to follow, so a text or mail stage switched live first
    changes nothing."""
    sid = str(ctx.stage.get("pipeline") or "").strip()
    if not sid or lane not in BOARD_LANES or not board_live(ctx.policy, sid):
        return ""
    return sid


def position(record_uuid: str) -> dict | None:
    """The record's card as the live board holds it: {column, card, entered, due, round_start ...}."""
    f = ledger.fact(record_uuid, FACT) if record_uuid else None
    return f if isinstance(f, dict) and f.get("column") else None


def _timeline(ctx: Ctx, lane: str) -> dict:
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    return cfg.timeline(lane) or cfg.timeline("ftm") or lanes.DEFAULT_TIMELINES["ftm"]


def _entered_day(p: dict, zone, today: date) -> date:
    try:
        return datetime.fromisoformat(p.get("entered") or "").astimezone(zone).date()
    except (TypeError, ValueError):
        return today


def _since(p: dict, today: date) -> str:
    return str(p.get("round_start") or p.get("due") or today.isoformat())


def text_slot(ctx: Ctx, record_uuid: str, lane: str, series: str, today: date) -> dict | None:
    """For the text lane (textlane.py): which day of the round this record is on, by its card.

    None             the stage does not follow a live board for this lane: lane days as before
    {"hold": why}    no text today (in Research, not on the board yet, the follow-ups not open)
    {"day", "counter", "max_step", "since"}
                     the lane day and call counter the timeline is asked with, the highest opening
                     text the column allows (Day N allows text N: a held card is not texted twice),
                     and the day the round's texts count from"""
    if not follows(ctx, lane):
        return None
    p = position(record_uuid)
    if not p:
        return {"hold": "not on the First to Market board yet"}
    tl = _timeline(ctx, lane)
    opening = sorted(tl.get("opening_texts") or [0])
    ends = int(tl.get("round_ends_at", DAYS))
    n = column_no(p["column"])
    since = _since(p, today)
    if n == 0:
        return {"hold": "in Research on the board: research has not named a decision maker with a number yet"}
    if n < 0:
        return {"hold": "in a column of the board the round does not use"}
    if series == "opening":
        if n <= DAYS:
            return {"day": opening[0] + n - 1, "counter": n - 1, "max_step": n, "since": since}
        return {"day": opening[-1] + 1, "counter": ends, "max_step": 0, "since": since}     # Exhausted: the opening texts close
    if n <= DAYS:
        return {"hold": "the follow-up texts start when the card reaches Exhausted"}
    if p.get("backlog"):
        return None           # its round ended before the board: the lane's own days, as they always were
    zone = ZoneInfo(ctx.policy.tz)
    fu = tl.get("followup_texts") or {}
    base_date, base_day = _entered_day(p, zone, today), int(fu.get("first_day", 8))
    # base_date and base_day put any earlier follow-up on the same scale as `day`, so the spacing
    # between two of them is measured in the board's days too.
    return {"day": base_day + lanes.business_days(base_date, today), "counter": ends, "since": since,
            "base_date": base_date.isoformat(), "base_day": base_day}


def mail_point(ctx: Ctx, record_uuid: str, lane: str, today: date) -> dict | None:
    """For the mail stage (mail.py): the lane day and counter the timeline is asked with. None for a
    record in Research or not on the board: it waits on research, and the lane's own days (mail at
    once with no number, then the eighth day) apply as they always did. On a day of the round the
    mail waits for Exhausted unless the timeline mails the record at once (a foreclosure)."""
    if not follows(ctx, lane):
        return None
    p = position(record_uuid)
    n = column_no((p or {}).get("column") or "")
    if not p or n <= 0:
        return None
    tl = _timeline(ctx, lane)
    opening = sorted(tl.get("opening_texts") or [0])
    if n <= DAYS:
        return {"day": opening[0] + n - 1, "counter": n - 1}
    if p.get("backlog"):
        return None           # its round ended before the board: the lane's own days, as they always were
    zone = ZoneInfo(ctx.policy.tz)
    return {"day": int(tl.get("mail_first_day", 8)) + lanes.business_days(_entered_day(p, zone, today), today),
            "counter": int(tl.get("round_ends_at", DAYS))}


def afternoon_day(ctx: Ctx, record_uuid: str, lane: str, today: date) -> dict | None:
    """For the afternoon text (fullattempt.py): the lane day the card's column stands for, and the day
    the round's texts count from. None when the stage keeps the lane days."""
    if not follows(ctx, lane):
        return None
    p = position(record_uuid)
    if not p:
        return {"hold": "not on the First to Market board yet"}
    n = column_no(p["column"])
    if not 1 <= n <= DAYS:
        return {"hold": "not on a day of the round"}
    opening = sorted(_timeline(ctx, lane).get("opening_texts") or [0])
    return {"day": opening[0] + n - 1, "since": _since(p, today)}


def counter_for(ctx: Ctx, record_uuid: str, lane: str) -> int | None:
    """For the attempt keeper (attempts.py): the call counter a record on the live board should read,
    the full attempts the board counted (Day 3 has had two; Exhausted has had the round). None when
    the stage keeps counting call days."""
    if not follows(ctx, lane):
        return None
    p = position(record_uuid)
    n = column_no((p or {}).get("column") or "")
    if not p or n < 0:
        return None
    if n > DAYS:
        return int(_timeline(ctx, lane).get("round_ends_at", DAYS))
    return max(0, n - 1)


def undo_create(crm, change: dict) -> str:
    """Put back a card this stage created (leadroutes.undo_step calls this for "card_create"): the
    card goes, from wherever it sits now. Its id is in the run's ledger event."""
    u = change.get("record_uuid") or ""
    ids = [(e["detail"].get("card"), e["detail"].get("column")) for e in ledger.events(u, action="card_create")
           if e["run_id"] == change.get("run_id") and e["detail"].get("card")]
    if not ids:
        raise RuntimeError("the card's id was not recorded, so it cannot be removed from here")
    now = position(u) or {}
    for card, col in ids:
        # A card is addressed by (column, card), and an undone move leaves the stage's memory one
        # column behind, so each column it can be in is asked first (a read), then it is removed.
        places = [now.get("column_uuid") if now.get("card") == card else "", col, (change.get("after") or {}).get("column")]
        for where in dict.fromkeys(p for p in places if p):
            if leadroutes.card_in_column(crm, where, card):
                leadroutes.remove_card(crm, where, card)
                break
        else:
            raise RuntimeError("the card is not in the column it was placed in or last moved to; take it off by hand")
        if now.get("card") == card:
            ledger.set_fact(u, FACT, None)
    return "%d card(s) removed" % len(ids)


# ---------------------------------------------------------------------- the run
def _facts(key: str) -> dict:
    """record uuid -> the fact, for every record that has one, in one read."""
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (key,)).fetchall():
            try:
                v = json.loads(r["value"])
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("column"):
                out[r["record_uuid"]] = v
    return out


def _nice(d: date) -> str:
    return d.strftime("%a %b %d").replace(" 0", " ")


class Run:
    """One pass over the board. Holds what the run read so the decisions below share it."""

    def __init__(self, ctx: Ctx, o: dict):
        self.ctx, self.o, self.st = ctx, o, ctx.stage
        self.zone = ZoneInfo(ctx.policy.tz)
        local = ctx.now.astimezone(self.zone)
        self.today = local.date()
        self.today_s = self.today.isoformat()
        self.days = ctx.policy.days
        self.lim = limits.settings(ctx.policy)
        self.first_call = int(self.lim.call_hours[0])
        minute = local.hour * 60 + local.minute
        self.morning = minute < self.first_call
        self.late = minute >= (_hm(o["late_after"]) or 12 * 60)
        night = minute < (_hm(o["night_before"]) or 0)
        self.reads = [0]
        self.read_cap = int(o["night_reads"] if night else o["max_reads"])
        self.key = FACT if ctx.live else PLAN_FACT
        self.cols: dict = {}          # column title -> uuid
        self.board_uuid = ""
        self.listed = False           # the board's cards were read this run
        self.pos: dict = {}           # record -> position
        self.rows: dict = {}          # record -> search row (with _lane)
        self.creates = self.moves = 0
        self.broken = False
        self.verified = False
        self.tally = ctx.out.tally
        self.moved: Counter = Counter()
        self.placed: Counter = Counter()
        self.held: Counter = Counter()
        self.short: Counter = Counter()
        self.off: Counter = Counter()
        st = ctx.policy.stages.get(str(o["text_stage"])) if hasattr(ctx.policy, "stages") else None
        self.text_live = {"always": True, "never": False}.get(
            str(o["require_text"]), bool(st is not None and st.mode == "live" and config.live()))

    # ---- reading
    def sync(self) -> None:
        for group in ("calls", "state", "board"):
            s = activity.sync(self.ctx.crm, group, now=self.ctx.now, floor_days=int(self.o["floor_days"]),
                              max_calls=int(self.o["max_log_calls"]))
            if not s["complete"]:
                self.ctx.out.problem("the %s log is still catching up (read through %s); a card with no call shown is held, "
                                     "and the next run reads further" % (group, s["through"] or "nothing yet"))

    def load_board(self) -> bool:
        """The board's columns, by title. From the playbook when it names all seven, else learned
        (leadroutes.load_board: the board detail, then a card's timeline). A live run with any column
        unknown does nothing; a plan run goes on and simulates the board."""
        o, ctx = self.o, self.ctx
        given = dict(o["column_ids"])
        problems: list = []
        b = leadroutes.load_board(ctx.crm, str(o["board"]), column_ids=given, expect=COLUMNS, now=ctx.now,
                                  problems=problems)
        self.board_uuid = b.uuid
        for title in COLUMNS:
            uid = given.get(title) or b.find(title)
            if uid:
                self.cols[title] = uid
        missing = [t for t in COLUMNS if t not in self.cols]
        if not missing:
            return True
        say = ("the %s board's columns %s are not known: run the account tool's apply (python src/agents/autopilot/"
               "pipelines.py), then put each column's id under column_ids in this stage's block" % (o["board"], ", ".join(missing)))
        if ctx.live:
            ctx.out.problem(say)
            for p in problems:
                ctx.out.problem(p)
            ctx.out.head = "The board's columns are not known yet, so no card was placed or moved."
            return False
        self.tally["plan only: the board is simulated (%s)" % ("its columns are not set yet" if self.cols else "it does not exist yet")] += 1
        return True

    def read_cards(self) -> None:
        """Where every card is now. Live: the board's own listing is the truth, merged with what this
        stage remembers (when it entered, when its attempt is due). A card a person moved is taken
        where they put it; a card a person removed is not put back. Plan: the simulation, with any
        real card on the board taking its place."""
        ctx, o = self.ctx, self.o
        mine = _facts(self.key)
        found: dict = {}
        unread: set = set()           # columns whose listing failed or may be cut short
        if len(self.cols) == len(COLUMNS):
            by_card = {v.get("card"): u for u, v in _facts(FACT).items() if v.get("card")}
            index = leadroutes.card_index()
            for title, col in self.cols.items():
                try:
                    listed = leadroutes.cards(ctx.crm, col, cap=int(o["max_cards"]))
                except Exception as exc:  # noqa: BLE001
                    ctx.out.problem("could not list the %s column (%s)" % (title, str(exc)[:100]))
                    unread.add(title)
                    continue
                if len(listed) >= int(o["max_cards"]):
                    # A full page may not be the whole column: what it does not show is unknown,
                    # never "taken off by a person".
                    unread.add(title)
                    ctx.out.problem("the %s column holds %d or more cards, the most one run lists; raise max_cards"
                                    % (title, int(o["max_cards"])))
                for c in listed:
                    cu = str(c.get("uuid") or "")
                    u = leadroutes.card_prop(c) or by_card.get(cu) or index.get(cu, "")
                    if not cu:
                        continue
                    if not u:
                        self.tally["cards whose record the stage cannot name (left alone)"] += 1
                        continue
                    found[u] = {"card": cu, "column": title, "column_uuid": col}
            self.listed = True
        if ctx.live:
            for u, f in mine.items():
                if u not in found and f.get("column") in unread:
                    self.pos[u] = f           # its column was not read in full: it is where it was
                elif u not in found:
                    # The card is gone from the board, and this stage did not take it: a person did.
                    ledger.set_fact(u, OFF_FACT, {"day": self.today_s, "column": f.get("column")})
                    ledger.set_fact(u, FACT, None)
                    self.tally["cards a person took off the board (not put back for %d days)" % int(o["off_days"])] += 1
            for u, c in found.items():
                f = dict(mine.get(u) or {})
                if f.get("card") == c["card"] and f.get("column") == c["column"]:
                    f["column_uuid"] = c["column_uuid"]
                    self.pos[u] = f
                    continue
                how = "found on the board" if not f else "moved by a person"
                if f:
                    self.tally["cards a person moved (taken where they put them)"] += 1
                entered = self._entered(u, c)
                f.update(card=c["card"], column=c["column"], column_uuid=c["column_uuid"],
                         entered=entered.isoformat(timespec="seconds"),
                         due=due_day(entered, self.zone, self.days, self.first_call).isoformat(), source=how)
                if column_no(c["column"]) == 1 and not f.get("round_start"):
                    f["round_start"] = f["due"]
                ledger.set_fact(u, FACT, f)
                self.pos[u] = f
            return
        self.pos = dict(mine)
        for u, c in found.items():
            f = dict(self.pos.get(u) or {})
            if f.get("column") != c["column"]:
                entered = self._entered(u, c)
                f.update(card=c["card"], column=c["column"], column_uuid=c["column_uuid"],
                         entered=entered.isoformat(timespec="seconds"),
                         due=due_day(entered, self.zone, self.days, self.first_call).isoformat(), source="on the board")
            self.pos[u] = f

    def _entered(self, u: str, c: dict) -> datetime:
        """When a card this stage did not place entered its column: the log copy's card event for that
        column, else the card's own timeline (one read), else now."""
        e = leadroutes.last_card_event(u)
        if e and str(e["data"].get("column") or "").strip().lower() in (c["column"].lower(), c["column_uuid"].lower()):
            return e["when"]
        if self.reads[0] < self.read_cap:
            try:
                tl = leadroutes.timeline(self.ctx.crm, c["column_uuid"], c["card"])
                self.reads[0] += 1
                cur = (tl.get(c["column_uuid"]) or {})
                if cur.get("seconds") is not None:
                    return self.ctx.now - timedelta(seconds=float(cur["seconds"]))
            except Exception:  # noqa: BLE001  (an unread timeline means "now", which only delays the card a day)
                pass
        return self.ctx.now

    def read_rows(self) -> None:
        """Every county record: the first-to-market presets (anchored on the FTM tag), then the
        obituary presets. A preset cannot say "tag FTM or list Obituary" (its keys are ANDed), so the
        Obituary list comes in through its own preset and the board is where the two meet."""
        cap = int(self.o["max_scan"])
        for title, row in preset_rows(self.ctx, self.st.presets, None, cap):
            self.rows.setdefault(row["uuid"], dict(row, _lane="ftm", _via=title))
        for title, row in preset_rows(self.ctx, self.o["obituary_presets"], None, cap):
            self.rows.setdefault(row["uuid"], dict(row, _lane="obituary", _via=title))

    def read(self, u: str) -> dict | None:
        if self.reads[0] >= self.read_cap:
            return None
        try:
            rec = self.ctx.crm.record(u)
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read a record (%s)" % str(exc)[:100])
            return None
        self.reads[0] += 1
        return rec if rec.get("uuid") else None

    # ---- writing
    def _save(self, u: str, f: dict | None) -> None:
        ledger.set_fact(u, self.key, f)
        if f is None:
            self.pos.pop(u, None)
        else:
            self.pos[u] = f

    def _first_holds(self, col_uuid: str, card: str, what: str) -> bool:
        """The first live write of a run is read back; one that did not hold stops the writing."""
        if not self.ctx.live or self.verified:
            return True
        self.verified = True
        if leadroutes.card_in_column(self.ctx.crm, col_uuid, card):
            return True
        self.broken = True
        self.ctx.out.problem("the first card %s did not hold (it is not in its column after the write); nothing more was "
                             "written this run. Check the column ids under column_ids" % what)
        return False

    def create(self, u: str, owner: str, title: str, why: str, lane: str, counter: int = 0) -> bool:
        ctx = self.ctx
        if self.broken:
            return False
        if self.creates >= int(self.o["max_creates"]):
            self.tally["waiting to be placed (the run's card limit; next run)"] += 1
            return False
        col = self.cols.get(title, "")
        card = "plan-%s" % u
        if ctx.live:
            ctx.change(u, "card_create", None, {"column": col, "board": self.board_uuid, "title": title})
            made = ctx.write("place card", leadroutes.create_card, ctx.crm, col, u) or {}
            card = str((made or {}).get("uuid") or "")
            if not card:
                card = next((str(c.get("uuid")) for c in leadroutes.cards(ctx.crm, col, cap=int(self.o["max_cards"]))
                             if leadroutes.card_prop(c) == u), "")
            if not card:
                self.broken = True
                ctx.out.problem("a card was sent to %s but its id could not be read back; nothing more was written" % title)
                return False
            ledger.event(ctx.run_id, self.st.id, u, "card_create", {"card": card, "column": col, "title": title},
                         now=ctx.now, tz=ctx.policy.tz)
            if not self._first_holds(col, card, "placed"):
                return False
        else:
            ctx.write("place card", leadroutes.create_card, ctx.crm, col, u)
        self.creates += 1
        due = due_day(ctx.now, self.zone, self.days, self.first_call)
        n = column_no(title)
        start = due if n == 1 else (back_business_days(due, n - 1, self.days) if 1 < n <= DAYS + 1 else None)
        # A record already called before the board went live starts where its counter says. One
        # placed straight into Exhausted finished its round before the board: its follow-ups and
        # mail keep the lane's own days, so an old round is not restarted with four new texts.
        backlog = counter > 0 and n > 1
        self._save(u, {"card": card, "column": title, "column_uuid": col, "entered": ctx.now.isoformat(timespec="seconds"),
                       "due": due.isoformat(), "moved_day": self.today_s, "owner": owner, "lane": lane,
                       "round_start": start.isoformat() if start else "", "checked": ctx.now.isoformat(timespec="seconds"),
                       "source": "placed", "why": why, "counter": counter, "backlog": backlog})
        self.placed[title] += 1
        return True

    def move(self, u: str, p: dict, title: str, why: str) -> bool:
        ctx = self.ctx
        if self.broken:
            return False
        if p.get("moved_day") == self.today_s:
            self.tally["already moved today (one column a business day)"] += 1
            return False
        if self.moves >= int(self.o["max_moves"]):
            self.tally["moves left for the next run (the run's move limit)"] += 1
            return False
        frm, to = p.get("column_uuid") or self.cols.get(p["column"], ""), self.cols.get(title, "")
        if ctx.live:
            ctx.change(u, "card_move", {"card": p["card"], "column": frm, "board": self.board_uuid, "title": p["column"]},
                       {"card": p["card"], "column": to, "title": title})
            ctx.write("move card", leadroutes.move_card, ctx.crm, frm, p["card"], to)
            ledger.event(ctx.run_id, self.st.id, u, "card_move", {"from": p["column"], "to": title, "why": why},
                         now=ctx.now, tz=ctx.policy.tz)
            if not self._first_holds(to, p["card"], "moved"):
                return False
        else:
            ctx.write("move card", leadroutes.move_card, ctx.crm, frm, p.get("card") or "", to)
        self.moves += 1
        due = due_day(ctx.now, self.zone, self.days, self.first_call)
        f = dict(p, column=title, column_uuid=to, entered=ctx.now.isoformat(timespec="seconds"), due=due.isoformat(),
                 moved_day=self.today_s, why=why)
        if column_no(title) == 1 and column_no(p["column"]) == 0:
            f["round_start"] = due.isoformat()
        self._save(u, f)
        self.moved["%s to %s" % (p["column"], title)] += 1
        return True

    def remove(self, u: str, p: dict, why: str, lead: bool) -> bool:
        ctx = self.ctx
        if self.broken:
            return False
        to = str(self.o["lead_column"] or "") if lead else ""
        if to and to != p["column"]:
            if self.move(u, p, to, why):
                self.off["a lead (moved to %s)" % to] += 1
                return True
            return False
        if to:
            return False
        col = p.get("column_uuid") or self.cols.get(p["column"], "")
        if ctx.live:
            ctx.change(u, "card_remove", {"card": p["card"], "column": col, "board": self.board_uuid, "title": p["column"]}, None)
            ctx.write("take card off", leadroutes.remove_card, ctx.crm, col, p["card"])
            ledger.event(ctx.run_id, self.st.id, u, "card_remove", {"from": p["column"], "why": why}, now=ctx.now, tz=ctx.policy.tz)
        else:
            ctx.write("take card off", leadroutes.remove_card, ctx.crm, col, p.get("card") or "")
        self._save(u, None)
        self.off[why if lead else _gone_word(why)] += 1
        return True

    # ---- deciding
    def _state_events(self, u: str, p: dict) -> bool:
        try:
            since = datetime.fromisoformat(p.get("checked") or p.get("entered") or "")
        except (TypeError, ValueError):
            return True
        return bool(activity.events(STATE_EVENTS, resource=u, since=since))

    def _stale(self, p: dict) -> bool:
        try:
            at = datetime.fromisoformat(p.get("checked") or "")
        except (TypeError, ValueError):
            return True
        return self.ctx.now - at >= timedelta(hours=float(self.o["recheck_hours"]))

    def _refresh(self, u: str, p: dict, rec: dict, row: dict | None) -> tuple[str, str, dict]:
        """Read what the record says now into the position: its state, whether a caller may dial it,
        whether a text may go to it, and its lane."""
        lanes_cfg = getattr(self.ctx.policy, "lanes", None) or lanes.Lanes()
        lane = lanes.lane_of(rec, lanes_cfg)
        custom = None
        if column_no(p.get("column") or "") == 0 or (marks_of(rec) & _low(self.o["research_first"])):
            custom = custom_of(self.ctx, u, self.reads)
        state, why = record_state(self.ctx, rec, self.o, custom)
        p = dict(p, lane=lane, owner=(rec.get("owner") or {}).get("uuid") or p.get("owner") or "",
                 callable=bool(numbers(self.ctx, rec, row, "call", lane)),
                 mobile=bool(numbers(self.ctx, rec, row, "text", lane)),
                 dm=dm_named(rec, custom, self.o), checked=self.ctx.now.isoformat(timespec="seconds"))
        return state, why, p

    def _needs_text(self, p: dict):
        if not self.text_live or p.get("lane") in self.o["call_alone_lanes"] or not p.get("mobile"):
            return lambda n: False
        days = set(self.o["text_days"])
        return lambda n: n in days

    def _evidence(self, p: dict, since: date) -> tuple[set, set]:
        owner = p.get("owner") or ""
        start = datetime.combine(since, datetime.min.time(), tzinfo=self.zone)
        return (dial_dates(owner, start, self.zone, self.lim.call_hours),
                text_dates(owner, since.isoformat()))

    def day_card(self, u: str, p: dict) -> None:
        """A card on a day of the round. Before calls start, the business days that are over are
        judged and the card moves the columns they earned (one a day at most) or holds, said with
        the day it missed. After calls start, only what today still lacks is said."""
        n = column_no(p["column"])
        try:
            due = date.fromisoformat(p.get("due") or "")
        except ValueError:
            due = self.today
        row = self.rows.get(u)
        events = self._state_events(u, p)
        if self.morning:
            until = self.today - timedelta(days=1)
            calls, texts = self._evidence(p, due)
            judged = [d for d in calls if due <= d <= until]
            if not judged and not events:
                if due > until:
                    self.tally["due today"] += 1
                elif p.get("callable") is False:
                    self.held["no number a caller may dial yet"] += 1
                else:
                    self.held["no call on %s" % _nice(back_business_days(self.today, 1, self.days))] += 1
                return
            if events or self._stale(p) or self.text_live:
                rec = self.read(u)
                if rec is None:
                    self.tally["not judged this run (read cap; next run)"] += 1
                    return
                state, why, p = self._refresh(u, p, rec, row)
                self._save(u, p)
                if state in ("lead", "gone"):
                    self.remove(u, p, why if state == "gone" else DONE_LEAD, state == "lead")
                    return
                if state == "unknown":
                    self.held[why] += 1
                    return
                if state == "research":
                    self.move(u, p, RESEARCH, why)
                    return
                if state == "exhausted":
                    self.move(u, p, EXHAUSTED, why)
                    return
            earned, misses = judge(n, due, until, calls, texts, self._needs_text(p), self.days)
            if earned > n:
                self.move(u, p, column_title(earned), "the full attempt on %s" % _nice(max(judged)))
                return
            if not p.get("callable"):
                self.held["no number a caller may dial yet"] += 1
            elif misses:
                d, what = misses[-1]
                self.held["%s on %s" % (what, _nice(d))] += 1
            else:
                self.tally["due today"] += 1
            return
        # After calls start: nothing on a day of the round moves; only what today lacks is said.
        if events:
            rec = self.read(u)
            if rec is not None:
                state, why, p = self._refresh(u, p, rec, row)
                self._save(u, p)
                if state in ("lead", "gone"):
                    self.remove(u, p, why if state == "gone" else DONE_LEAD, state == "lead")
                    return
        if due > self.today:
            self.tally["due on the next business day"] += 1
            return
        if p.get("callable") is False:
            self.short["no number a caller may dial yet"] += 1
            return
        calls, texts = self._evidence(p, due)
        need = self._needs_text(p)(n)
        called = self.today in calls
        texted = any(due <= t <= self.today for t in texts)
        if called and (texted or not need):
            self.tally["today's full attempt is in"] += 1
        elif not called:
            self.short["no call yet today on %s" % p["column"]] += 1
        else:
            self.short["no text yet on %s" % p["column"]] += 1

    def research_card(self, u: str, p: dict, ready_only: bool = False) -> None:
        """A card in Research moves to Day 1 once research has named a decision maker and the record
        has a number a caller may dial; until then it waits, read about once a day."""
        if not (self._state_events(u, p) or self._stale(p)):
            self.tally["in Research (unchanged since it was read)"] += 1
            return
        rec = self.read(u)
        if rec is None:
            self.tally["not judged this run (read cap; next run)"] += 1
            return
        state, why, p = self._refresh(u, p, rec, self.rows.get(u))
        self._save(u, p)
        if state in ("lead", "gone"):
            self.remove(u, p, why if state == "gone" else DONE_LEAD, state == "lead")
            return
        if state != "work":
            self.tally["in Research: %s" % why] += 1
            return
        first = marks_of(rec) & _low(self.o["research_first"])
        if not p.get("dm") and first:
            self.tally["in Research: no decision maker named yet"] += 1
            return
        if not p.get("callable"):
            self.tally["in Research: no number a caller may dial yet"] += 1
            return
        self.move(u, p, "Day 1", "research named a decision maker (%s) with a number" % (p.get("dm") or "the owner"))

    def exhausted_card(self, u: str, p: dict) -> None:
        if not self._state_events(u, p):
            self.tally["in Exhausted"] += 1
            return
        rec = self.read(u)
        if rec is None:
            self.tally["not judged this run (read cap; next run)"] += 1
            return
        state, why, p = self._refresh(u, p, rec, self.rows.get(u))
        self._save(u, p)
        if state in ("lead", "gone"):
            self.remove(u, p, why if state == "gone" else DONE_LEAD, state == "lead")
            return
        self.tally["in Exhausted"] += 1

    def place_new(self) -> None:
        """Every county record with no card gets one, first-to-market rows first."""
        o = self.o
        try:
            off_cut = (self.today - timedelta(days=int(o["off_days"]))).isoformat()
        except (TypeError, ValueError):
            off_cut = self.today_s
        for u, row in self.rows.items():
            if u in self.pos:
                continue
            off = ledger.fact(u, OFF_FACT)
            if isinstance(off, dict) and str(off.get("day") or "") >= off_cut:
                self.tally["taken off the board by a person (left off)"] += 1
                continue
            s = status_of(row)
            if s in lanes.LEAD_STATUSES:
                self.tally["not placed: %s" % DONE_LEAD] += 1
                continue
            if self.broken or self.creates >= int(o["max_creates"]):
                self.tally["waiting to be placed (the run's card limit; next run)"] += 1
                continue
            rec = self.read(u)
            if rec is None:
                self.tally["waiting to be placed (the run's read limit; next run)"] += 1
                continue
            lane = lanes.lane_of(rec, getattr(self.ctx.policy, "lanes", None) or lanes.Lanes())
            custom = custom_of(self.ctx, u, self.reads) if marks_of(rec) & _low(o["research_first"]) else None
            title, why = placement(self.ctx, rec, row, o, custom, lane)
            if not title:
                self.tally["not placed: %s" % _gone_word(why)] += 1
                continue
            owner = (rec.get("owner") or {}).get("uuid") or ""
            if self.create(u, owner, title, why, lane, int(rec.get("predictivecall_attempts") or 0)):
                p = self.pos.get(u) or {}
                p.update(callable=bool(numbers(self.ctx, rec, row, "call", lane)),
                         mobile=bool(numbers(self.ctx, rec, row, "text", lane)), dm=dm_named(rec, custom, o))
                self._save(u, p)

    def research_waits(self) -> list[str]:
        """What the research queue knows about the cards in Research, in one read of its tables."""
        from . import researchqueue
        waiting = [u for u, p in self.pos.items() if column_no(p.get("column") or "") == 0]
        if not waiting:
            return []
        idx = researchqueue.research_index()
        seen = researchqueue.facts_by_key(researchqueue.SIG_FACT)
        waits = researchqueue.facts_by_key(researchqueue.WAIT_FACT)
        c = Counter()
        for u in waiting:
            s = idx.get(u) or {}
            if s.get("open"):
                c["being researched"] += 1
            elif s.get("done"):
                c["researched, waiting on a number or a decision maker"] += 1
            elif u in waits:
                c["waiting their turn in the research queue"] += 1
            elif u in seen:
                c["ranked by the research queue"] += 1
            else:
                c["not seen by the research queue yet"] += 1
        return ["In Research: %d (%s)." % (len(waiting), ", ".join("%s %d" % (k, v) for k, v in c.most_common()))]


def _gone_word(why: str) -> str:
    """A reason in the few words the run's head uses, so one morning's removals group together."""
    w = str(why or "")
    low = w.lower()
    if "lead" in low:
        return DONE_LEAD
    for key, say in (("sold", "the house sold"), ("do not market", "Do Not Market"), ("opted out", "the owner opted out"),
                     ("litigator", "a litigator"), ("single-family", "not a single-family house"),
                     ("mls", "listed on the MLS"), ("county record", "no longer a county record"),
                     ("house to market", "research found the wrong house")):
        if key in low:
            return say
    return w or "off the board"


def run_ftmpipeline(ctx: Ctx) -> None:
    o = options(ctx.stage)
    r = Run(ctx, o)
    if r.today.weekday() not in r.days:
        ctx.out.head = "%s is not a business day; the board waits for the next one." % r.today.strftime("%A")
        return
    r.sync()
    if not r.load_board():
        return
    r.read_cards()
    r.read_rows()
    ctx.out.found = len(set(r.pos) | set(r.rows))
    # Cards first, lowest day first, so a card that earned a column today is not behind the backlog.
    order = sorted(r.pos.items(), key=lambda kv: (column_no(kv[1].get("column") or ""), kv[0]))
    for u, p in order:
        n = column_no(p.get("column") or "")
        if n < 0:
            ctx.out.tally["cards in a column the round does not use (left alone)"] += 1
        elif n == 0:
            r.research_card(u, p)
        elif n <= DAYS:
            r.day_card(u, p)
        else:
            r.exhausted_card(u, p)
    r.place_new()
    _report(ctx, r)


def _report(ctx: Ctx, r: Run) -> None:
    now_cols = Counter(p.get("column") for p in r.pos.values())
    board = ", ".join("%s %d" % (t, now_cols[t]) for t in COLUMNS if now_cols.get(t))
    words = (("placed", "moved", "took"), ("place", "move", "take"))[0 if ctx.live else 1]
    parts = []
    if r.placed:
        parts.append("%s %d card(s) (%s)" % (words[0], sum(r.placed.values()),
                                             ", ".join("%s %d" % (t, r.placed[t]) for t in COLUMNS if r.placed.get(t))))
    if r.moved:
        parts.append("%s %d (%s)" % (words[1], sum(r.moved.values()), ", ".join("%s %d" % kv for kv in sorted(r.moved.items()))))
    if r.off:
        parts.append("%s %d off the board (%s)" % (words[2], sum(r.off.values()),
                                                   ", ".join("%s %d" % kv for kv in r.off.most_common())))
    ctx.out.acted = sum(r.placed.values()) + sum(r.moved.values()) + sum(r.off.values())
    if r.late:
        # The late pass is for people: a card still short of today's call or text is work while
        # there is time, so it counts, and the run is posted rather than read as a quiet one.
        ctx.out.acted += sum(r.short.values())
    pass_word = "the late pass" if r.late else ("before calls" if r.morning else "after calls started")
    if parts:
        text = "; ".join(parts) + "."
        head = text[0].upper() + text[1:] if ctx.live else "Would " + text
    else:
        head = "Nothing to place or move (%s)." % pass_word
    if r.held:
        head += " Held %d: %s." % (sum(r.held.values()), ", ".join("%s %d" % kv for kv in r.held.most_common(4)))
    if r.short:
        head += " Short today: %s." % ", ".join("%s %d" % kv for kv in r.short.most_common(5))
    ctx.out.head = head
    if board:
        ctx.out.line("On the board: %s." % board)
    if not r.text_live and str(r.o["require_text"]) == "auto":
        ctx.out.line("The county texts are not live yet, so a day's call alone completes it; once %s is live, Days %s also "
                     "need that day's text." % (r.o["text_stage"], ", ".join(str(d) for d in r.o["text_days"]) or "none"))
    hot = sum(1 for p in r.pos.values() if p.get("column") == EXHAUSTED and p.get("lane") == "hot_auction")
    if hot:
        ctx.out.line("%d card(s) in Exhausted are hot auctions: their lane keeps a call task every few days until the sale."
                     % hot)
    for ln in r.research_waits():
        ctx.out.line(ln)
