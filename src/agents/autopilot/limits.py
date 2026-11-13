"""Outreach limits, enforced (build plan 4.6; decisions D32 and D42).

The limits were house rules nobody could check: 25 calls and 25 texts per number a day, 10 numbers
per caller, one call per owner a day, one text per owner a day and five a week, calls from 9am to
7pm and machine sends from 9am to 6pm, on the business clock AND on the owner's own. Measured on
2026-11-12: three caller ID lines carried 28, 29 and 32 dials in one day and one caller rang out from
30 numbers, and nothing said so.

Two halves live here.

BEFORE THE SEND: room(). Every text stage and call stage asks room(owner, channel, when) before it
stages anything, so a limit stops the touch instead of describing it the next morning. For a text
it answers: is the number on the do-not-call registry, is `when` inside the sending hours on both
clocks, has this owner already had a text today or five in the last seven days, is the texting
platform resting this contact, and does the owner already have a text thread somewhere else.

AFTER THE DAY: the nightly check. It reads the day's dials from the shared call log and the day's
texts from the ledger below and names every limit that was broken, by caller first name and by the
last four digits of a number. Never a whole number, never a homeowner's name.

THE TEXT LEDGER (ap_texts). Texts sent through Smarter Contact never reach DataSift's log, so the
autopilot keeps its own count: one row per number per text, written when a batch is staged and
confirmed when a person releases it. A row that was staged and never released stops counting when
its day is over, because a stale batch cannot be released.

ACROSS BOTH TEXTING SYSTEMS. The in-house sender raises the record's own text counter by one for
every text it sends. Each time a stage reads a record it compares that counter with the last value
it saw: an increase the autopilot did not make is a text from the other system, and it counts
toward the owner's day and week. The autopilot only knows the text happened between the two looks,
so it is dated halfway between them. A counter value met for the first time is history of unknown
date and is not counted.

ONE TEXT IS ONE MESSAGE TO ONE OWNER. An owner with three mobile numbers gets the same message on
each (nobody knows yet which one is theirs), and that is one text against the owner's day and week.

THE PLATFORM'S OWN RULE. Smarter Contact sends a contact at most three campaign texts in seven
days and then rests the contact for fourteen. A text scheduled into that rest is not sent, it
vanishes, and the ledger would count a text nobody received. So the same rule is applied here
first (platform_cap, platform_window_days, platform_rest_days; 0 switches it off). The three
opening texts on days 0, 1 and 2 use the whole allowance, so the next campaign text can go on
calendar day 21 at the earliest. A one-off text typed into an existing thread is not a campaign
send and is not held by this rule.

The settings are read from the playbook's `limits` stage block, whatever that stage's mode: the
numbers are the house rules, and switching the nightly report off does not lift them.
"""
from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
from . import activity, gate, ledger, scrub

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, day TEXT, owner_uuid TEXT, record_uuid TEXT,
    number TEXT, system TEXT, campaign INTEGER DEFAULT 1, series TEXT, step INTEGER DEFAULT 0,
    touch TEXT, batch_id INTEGER DEFAULT 0, stage TEXT, thread TEXT, status TEXT,
    usd REAL DEFAULT 0, detail TEXT);
CREATE INDEX IF NOT EXISTS ap_texts_owner ON ap_texts(owner_uuid, day);
CREATE INDEX IF NOT EXISTS ap_texts_batch ON ap_texts(batch_id, record_uuid);
CREATE INDEX IF NOT EXISTS ap_texts_day ON ap_texts(day, status);
"""
_READY: set = set()
OTHER = "other"                  # a text the record's own counter shows and the autopilot did not send
COUNTED = ("staged", "released")
LATE_START = 12 * 60             # the late sending group starts at noon on the business clock
SEEN_FACT = "sms_seen"


def ensure() -> None:
    key = str(config.DB_PATH)
    if key in _READY:
        return
    with store._LOCK, store._db() as c:
        c.executescript(SCHEMA)
    _READY.add(key)


# ---------------------------------------------------------------------- settings
@dataclass
class Limits:
    call_hours: tuple = (9 * 60, 19 * 60)      # people call 9am to 7pm, on both clocks
    text_hours: tuple = (9 * 60, 18 * 60)      # what the machine sends goes out 9am to 6pm, on both clocks
    calls_per_line_day: int = 25
    texts_per_line_day: int = 25
    lines_per_caller: int = 10
    text_lines: int = 10                       # how many sending numbers carry the day's texts
    monthly_texts: int = 20000
    owner_texts_day: int = 1
    owner_texts_week: int = 5
    owner_dials_number_day: int = 1            # one call per owner a day: a number is not dialed twice
    platform_cap: int = 3
    platform_window_days: int = 7
    platform_rest_days: int = 14
    count_record_counter: bool = True

    @property
    def day_capacity(self) -> int:
        return max(0, int(self.texts_per_line_day) * int(self.text_lines))


INT_KEYS = ("calls_per_line_day", "texts_per_line_day", "lines_per_caller", "text_lines", "monthly_texts",
            "owner_texts_day", "owner_texts_week", "owner_dials_number_day", "platform_cap", "platform_window_days",
            "platform_rest_days")


def _minutes(text) -> tuple[int, int]:
    a, b = str(text).split("-")
    ha, ma = (int(x) for x in a.split(":"))
    hb, mb = (int(x) for x in b.split(":"))
    start, end = ha * 60 + ma, hb * 60 + mb
    if not 0 <= start < end <= 24 * 60:
        raise ValueError(text)
    return start, end


def _block(policy):
    for st in (getattr(policy, "stages", None) or {}).values():
        if getattr(st, "kind", "") == "limits":
            return st
    return None


def read(policy) -> tuple[Limits, list[str]]:
    """(the limits, what is wrong with the playbook's block). A value that cannot be read keeps the
    default and is said out loud: a typo must never read as "no limit"."""
    st = _block(policy)
    if st is None:
        return Limits(), []
    return read_block(st)


def block_problems(st) -> list[str]:
    """What is wrong with a limits stage's block, for the playbook checker."""
    return ["stage %s: %s" % (st.id, p) for p in read_block(st)[1]]


def read_block(st) -> tuple[Limits, list[str]]:
    out, bad = Limits(), []
    for key in ("call_hours", "text_hours"):
        v = st.get(key)
        if v is None:
            continue
        try:
            setattr(out, key, _minutes(v))
        except (ValueError, TypeError):
            bad.append("limits.%s %r must read like 09:00-18:00; the default is kept" % (key, v))
    for key in INT_KEYS:
        v = st.get(key)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            bad.append("limits.%s must be a whole number; the default %d is kept" % (key, getattr(out, key)))
            continue
        setattr(out, key, v)
    if st.get("count_record_counter") is not None:
        out.count_record_counter = bool(st.get("count_record_counter"))
    return out, bad


def settings(policy) -> Limits:
    return read(policy)[0]


def problems(policy) -> list[str]:
    return read(policy)[1]


# ---------------------------------------------------------------------- clocks
_ZONE_FN: list = []


def zone_for(number: str) -> ZoneInfo | None:
    """The owner's time zone by area code, from the table the texting app already keeps
    (sms_agent.sender_pool.timezone_for). None when that table cannot be loaded on this box; a
    caller then treats the number as belonging to the late group, never to the early one."""
    if not _ZONE_FN:
        try:
            from sms_agent.sender_pool import timezone_for
            _ZONE_FN.append(timezone_for)
        except Exception as exc:  # noqa: BLE001
            log.warning("the area code table could not be loaded (%s); owner clocks are not checked", str(exc)[:120])
            _ZONE_FN.append(None)
    fn = _ZONE_FN[0]
    n = gate.digits(number)
    if fn is None or not n:
        return None
    return fn(n)


def zones_known() -> bool:
    """Whether the area code table loaded on this box. A stage says so when it did not: a number
    whose zone is unknown is texted only in the late group, and nobody's own clock is checked."""
    zone_for("8655550100")
    return bool(_ZONE_FN and _ZONE_FN[0] is not None)


def _mins(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def _hours_text(hours: tuple) -> str:
    def one(m):
        h, mm = divmod(int(m), 60)
        return "%d%s%s" % (h % 12 or 12, (":%02d" % mm) if mm else "", "am" if h < 12 else "pm")
    return "%s to %s" % (one(hours[0]), one(hours[1]))


def inside_hours(hours: tuple, when: datetime, tz: str, number: str = "", channel: str = "text") -> tuple[bool, str]:
    """Is `when` inside the hours on the business clock and, when a number is given, on the owner's
    own clock too? (yes or no, why not)."""
    local = when.astimezone(ZoneInfo(tz))
    if not hours[0] <= _mins(local) < hours[1]:
        return False, "%s is outside %s on the business clock" % (_clock(local), _hours_text(hours))
    n = gate.digits(number)
    if not n:
        return True, ""
    zone = zone_for(n)
    if zone is None:
        if channel == "text" and _mins(local) < LATE_START:
            return False, "the owner's time zone is not known, so the text waits for the late group"
        return True, ""
    own = when.astimezone(zone)
    if not hours[0] <= _mins(own) < hours[1]:
        return False, "it is %s where the number ending %s is, outside %s there" % (_clock(own), n[-4:], _hours_text(hours))
    return True, ""


def planned_send(number: str, day: date, lim: Limits, tz: str, not_before: datetime | None = None) -> tuple[str, datetime | None, str]:
    """Where a campaign text to this number goes today: (group, when, why not).

    The texting platform's delivery window runs on the account's clock, so a campaign is split in
    two by the owner's area code, the way the launcher already does it: the east group starts when
    the window opens, everyone else at noon (11am Central, 9am Pacific). A number whose own morning
    has not started by noon on the business clock fits neither group and is left out."""
    zone = ZoneInfo(tz)
    n = gate.digits(number)
    own = zone_for(n)
    opens = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=lim.text_hours[0])
    closes = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=lim.text_hours[1])
    late = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=max(LATE_START, lim.text_hours[0]))
    east = own is not None and opens.astimezone(own).utcoffset() == opens.utcoffset()
    group, start = ("east", opens) if east else ("other", late)
    if not_before is not None and not_before > start:
        start = not_before
    if start >= closes:
        return "", None, "today's sending window has closed"
    ok, why = inside_hours(lim.text_hours, start, tz, n, "text")
    if not ok:
        return "", None, why
    return group, start, ""


# ---------------------------------------------------------------------- the text ledger
def _day(when: datetime, tz: str) -> str:
    return when.astimezone(ZoneInfo(tz)).date().isoformat()


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def stage_text(*, stage: str, batch_id: int, owner_uuid: str, record_uuid: str, numbers: list[dict], system: str,
               series: str, step: int, touch: str, tz: str, campaign: bool = True, usd_each: float = 0.0,
               when: datetime | None = None) -> int:
    """One staged row per number of one text. `numbers` carry {"number", "thread", "send_at"}.
    Staged rows hold the owner's place for the day, so two stages cannot both pick the owner up."""
    ensure()
    rows = []
    for n in numbers or []:
        at = n.get("send_at") or (_iso(when) if when else store.now())
        at_dt = datetime.fromisoformat(at)
        rows.append((at, _day(at_dt, tz), owner_uuid or "", record_uuid or "", gate.digits(n.get("number")), system,
                     1 if campaign else 0, series, int(step), touch, int(batch_id), stage, n.get("thread") or "",
                     "staged", round(float(usd_each or 0), 4), json.dumps({"tier": n.get("tier") or "", "group": n.get("group") or ""})))
    with store._LOCK, store._db() as c:
        c.executemany("INSERT INTO ap_texts (at, day, owner_uuid, record_uuid, number, system, campaign, series, step, touch, "
                      "batch_id, stage, thread, status, usd, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def release_text(batch_id: int, record_uuid: str, keep_numbers) -> int:
    """A person released the batch: this record's staged rows become released for the numbers that
    still passed, and dropped for the rest."""
    ensure()
    keep = {gate.digits(n) for n in keep_numbers or []} - {""}
    done = 0
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT id, number FROM ap_texts WHERE batch_id=? AND record_uuid=? AND status='staged'",
                         (int(batch_id), record_uuid)).fetchall()
        for r in rows:
            ok = r["number"] in keep
            c.execute("UPDATE ap_texts SET status=? WHERE id=?", ("released" if ok else "dropped", r["id"]))
            done += ok
    return done


def drop_text(batch_id: int, record_uuid: str = "", status: str = "dropped") -> int:
    """Take a batch's staged rows (one record's, or all of them) out of the count."""
    ensure()
    q, args = "UPDATE ap_texts SET status=? WHERE batch_id=? AND status='staged'", [status, int(batch_id)]
    if record_uuid:
        q += " AND record_uuid=?"
        args.append(record_uuid)
    with store._LOCK, store._db() as c:
        return c.execute(q, args).rowcount


def rows(owner_uuid: str = "", since_day: str = "", until_day: str = "", batch_id: int = 0, statuses=COUNTED,
         record_uuid: str = "") -> list[dict]:
    ensure()
    q, args = "SELECT * FROM ap_texts WHERE 1=1", []
    if owner_uuid:
        q += " AND owner_uuid=?"
        args.append(owner_uuid)
    if record_uuid:
        q += " AND record_uuid=?"
        args.append(record_uuid)
    if since_day:
        q += " AND day>=?"
        args.append(since_day)
    if until_day:
        q += " AND day<?"
        args.append(until_day)
    if batch_id:
        q += " AND batch_id=?"
        args.append(int(batch_id))
    if statuses:
        q += " AND status IN (%s)" % ",".join("?" * len(statuses))
        args += list(statuses)
    with store._LOCK, store._db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY at, id", args).fetchall()]


def touches(owner_uuid: str, today: str, since_day: str = "", exclude_batch: int = 0, series: str = "",
            released_only: bool = False) -> list[dict]:
    """The owner's texts that count, one entry per text (not per number), oldest first. A released
    text always counts; a staged one counts only for today or later, because a batch that was not
    released on its day can no longer be."""
    out: dict[str, dict] = {}
    for r in rows(owner_uuid, since_day=since_day):
        if exclude_batch and r["batch_id"] == exclude_batch:
            continue
        if r["status"] == "staged" and (released_only or r["day"] < today):
            continue
        if series and r["series"] != series:
            continue
        t = out.setdefault(r["touch"] or "row-%d" % r["id"], {
            "touch": r["touch"], "day": r["day"], "at": r["at"], "system": r["system"], "campaign": bool(r["campaign"]),
            "series": r["series"], "step": r["step"], "thread": r["thread"], "status": r["status"], "batch_id": r["batch_id"],
            "record_uuid": r["record_uuid"], "numbers": []})
        if r["number"]:
            t["numbers"].append(r["number"])
    return sorted(out.values(), key=lambda t: (t["at"], t["touch"] or ""))


def thread_of(owner_uuid: str, today: str = "", exclude_batch: int = 0) -> dict | None:
    """The owner's first text the autopilot knows the thread of: {"system", "thread", "day"}. An
    owner keeps that thread for good (decision D42)."""
    for t in touches(owner_uuid, today or "9999-12-31", exclude_batch=exclude_batch):
        if t["system"] != OTHER:
            return {"system": t["system"], "thread": t["thread"], "day": t["day"], "numbers": list(t["numbers"])}
    return None


def number_threads(owner_uuid: str, since_day: str = "") -> dict:
    """number -> {"thread", "day", "system"} of the first text to it that was actually handed over.
    A number is in an owner's thread once a text to it was released, not while it is only staged."""
    out: dict[str, dict] = {}
    for r in rows(owner_uuid, since_day=since_day, statuses=("released",)):
        if r["number"] and r["system"] != OTHER:
            out.setdefault(r["number"], {"thread": r["thread"], "day": r["day"], "system": r["system"]})
    return out


def number_busy(number: str, day: str, owner_uuid: str = "") -> bool:
    """Is a text to this number already staged or released on this day for ANOTHER owner? One phone
    is one contact in the texting platform, and two owners can share a number (a relative, a
    business line): without this the same phone would get two texts in a day under two names."""
    n = gate.digits(number)
    if not n:
        return False
    ensure()
    with store._LOCK, store._db() as c:
        row = c.execute("SELECT 1 FROM ap_texts WHERE day=? AND number=? AND owner_uuid<>? AND status IN ('staged','released') "
                        "LIMIT 1", (day, n, owner_uuid or "")).fetchone()
    return row is not None


def messages(day: str = "", month: str = "", today: str = "", exclude_batch: int = 0) -> int:
    """Messages (one per number) the autopilot staged for today or released, on a day or in a month.
    `exclude_batch` leaves out the batch being released, so a batch that fills the day is not
    refused at its own release for filling the day."""
    ensure()
    q, args = "SELECT day, status FROM ap_texts WHERE system<>? AND status IN ('staged','released')", [OTHER]
    if day:
        q += " AND day=?"
        args.append(day)
    if month:
        q += " AND day LIKE ?"
        args.append(month + "-%")
    if exclude_batch:
        q += " AND batch_id<>?"
        args.append(int(exclude_batch))
    with store._LOCK, store._db() as c:
        return sum(1 for r in c.execute(q, args).fetchall() if r["status"] == "released" or r["day"] >= (today or r["day"]))


def spent(day: str, stage: str = "") -> float:
    """What the texts staged or released on a day are expected to cost. Text money is kept here and
    not on the shared spend ledger, where it would count against the data cap that traces and
    scoring live on."""
    ensure()
    q, args = "SELECT COALESCE(SUM(usd),0) AS s FROM ap_texts WHERE day=? AND status IN ('staged','released')", [day]
    if stage:
        q += " AND stage=?"
        args.append(stage)
    with store._LOCK, store._db() as c:
        return float(c.execute(q, args).fetchone()["s"] or 0)


# ---------------------------------------------------------------------- the other texting system
def observe(owner_uuid: str, rec: dict, when: datetime, tz: str, lim: Limits | None = None) -> int:
    """Compare the record's own text counter with the last value seen. An increase the autopilot
    did not make is a text from the other system: it is written to the ledger, dated halfway
    between the two looks, and counts toward the owner's day and week. Returns how many were found.
    What the counter read is knowledge about the record, so it is remembered in plan mode too."""
    if lim is not None and not lim.count_record_counter:
        return 0
    u = (rec or {}).get("uuid") or ""
    if not u or not owner_uuid:
        return 0
    try:
        total = int(rec.get("sms_attempts") or 0)
    except (TypeError, ValueError):
        return 0
    seen = ledger.fact(u, SEEN_FACT)
    stamp = _iso(when)
    if not isinstance(seen, dict):
        ledger.set_fact(u, SEEN_FACT, {"total": total, "at": stamp})      # first look: history of unknown date
        return 0
    try:
        last, last_at = int(seen.get("total") or 0), datetime.fromisoformat(seen.get("at") or stamp)
    except (TypeError, ValueError):
        last, last_at = total, when
    delta = total - last
    if delta <= 0:
        if delta < 0:
            ledger.set_fact(u, SEEN_FACT, {"total": total, "at": stamp})  # a person lowered the counter
        return 0
    mid = last_at + (when - last_at) / 2
    ensure()
    with store._LOCK, store._db() as c:
        for i in range(delta):
            c.execute("INSERT INTO ap_texts (at, day, owner_uuid, record_uuid, number, system, campaign, series, step, touch, "
                      "batch_id, stage, thread, status, usd, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (_iso(mid), _day(mid, tz), owner_uuid, u, "", OTHER, 0, "", 0, "counter:%s:%d" % (u, last + i + 1), 0,
                       "", "", "released", 0, json.dumps({"seen": stamp})))
    ledger.set_fact(u, SEEN_FACT, {"total": total, "at": stamp})
    return delta


def own_bump(record_uuid: str, total: int, when: datetime) -> None:
    """The autopilot just wrote the record's text counter itself: remember the new value, so the
    next look does not read its own write as a text from the other system."""
    if record_uuid:
        ledger.set_fact(record_uuid, SEEN_FACT, {"total": int(total), "at": _iso(when)})


# ---------------------------------------------------------------------- room: the check before the send
@dataclass
class Room:
    ok: bool
    why: str = ""
    code: str = ""           # hours, registry, thread, day, week, platform, month, capacity, dialed, owner
    today: int = 0
    week: int = 0
    thread: str = ""
    system: str = ""
    clear_on: str = ""       # the first day the platform will send this contact a campaign text again

    def __bool__(self) -> bool:
        return self.ok


def platform_clear(days: list[str], lim: Limits) -> str:
    """The first day a campaign text may go again, given the days campaign texts already went, or
    "" when the platform is not resting the contact. Read the strict way: once `platform_cap` texts
    land inside one window, nothing until the window ends plus the rest."""
    cap, win, rest = int(lim.platform_cap), int(lim.platform_window_days), int(lim.platform_rest_days)
    if cap <= 0 or len(days) < cap:
        return ""
    ds = sorted(date.fromisoformat(d) for d in days)
    clear = None
    for i in range(len(ds) - cap + 1):
        if ds[i + cap - 1] < ds[i] + timedelta(days=win):
            until = ds[i] + timedelta(days=win + rest)
            clear = until if clear is None or until > clear else clear
    return clear.isoformat() if clear else ""


def room(owner_uuid: str, channel: str, when: datetime, *, policy=None, lim: Limits | None = None, tz: str = "",
         number: str = "", system: str = "", campaign: bool = True, exclude_batch: int = 0,
         check_hours: bool = True) -> Room:
    """Is there room for one more touch to this owner at `when`?

    channel "text": one message to the owner (to every number of theirs that gets it). `system` is
    the texting system the message would go out on, `campaign` False for a one-off typed into an
    existing thread, `exclude_batch` the batch being released (its own staged rows are not counted
    against it).

    channel "call": with `number`, may that number be dialed (it has not been dialed today); with
    no number, may the owner be put on a call list (nobody has dialed them today). The answer is
    read from the shared copy of the call log as it stands, so a stage brings that copy up to date
    (activity.sync) before it asks.
    """
    if channel not in ("text", "call"):
        raise ValueError("room() answers for a text or a call, not %r" % channel)
    lim = lim or settings(policy)
    tz = tz or getattr(policy, "tz", "") or ledger.BUSINESS_TZ
    zone = ZoneInfo(tz)
    local = when.astimezone(zone)
    today = local.date()
    num = gate.digits(number)
    if not owner_uuid:
        return Room(False, "the record names no owner, so its touches cannot be counted", "owner")
    if channel == "call":
        if num and scrub.settings(policy).calls == "hold" and scrub.is_flagged(num):
            return Room(False, "the number ending %s is on the do-not-call registry: held from calls until the attorney "
                               "answers" % num[-4:], "registry")
        if check_hours:
            ok, why = inside_hours(lim.call_hours, when, tz, num, "call")
            if not ok:
                return Room(False, why, "hours")
        start = datetime(today.year, today.month, today.day, tzinfo=zone)
        dials = [e for e in activity.events(activity.DIAL, resource=owner_uuid, since=start, until=start + timedelta(days=1))
                 if e["data"].get("direction") != "inbound"]
        if num:
            n = sum(1 for e in dials if e["data"].get("number") == num)
            if n >= int(lim.owner_dials_number_day):
                return Room(False, "the number ending %s was already dialed today" % num[-4:], "dialed", today=len(dials))
        elif dials:
            return Room(False, "the owner was already called today", "dialed", today=len(dials))
        return Room(True, today=len(dials))

    if num and scrub.is_flagged(num):
        return Room(False, "the number ending %s is on the do-not-call registry" % num[-4:], "registry")
    if check_hours:
        days_ok = getattr(policy, "days", None)
        if days_ok is not None and local.weekday() not in days_ok:
            return Room(False, "%s is not a sending day" % local.strftime("%A"), "hours")
        ok, why = inside_hours(lim.text_hours, when, tz, num, "text")
        if not ok:
            return Room(False, why, "hours")
    look_back = max(7, int(lim.platform_window_days) + int(lim.platform_rest_days) + 1)
    mine = touches(owner_uuid, today.isoformat(), since_day=(today - timedelta(days=look_back)).isoformat(),
                   exclude_batch=exclude_batch)
    first = thread_of(owner_uuid, today.isoformat(), exclude_batch=exclude_batch)
    week_from = (today - timedelta(days=6)).isoformat()
    n_today = sum(1 for t in mine if t["day"] == today.isoformat())
    n_week = sum(1 for t in mine if week_from <= t["day"] <= today.isoformat())
    out = Room(True, today=n_today, week=n_week, thread=(first or {}).get("thread", ""), system=(first or {}).get("system", ""))
    if first and system and first["system"] != system:
        out.ok, out.code = False, "thread"
        out.why = "this owner's text thread is on %s: an owner keeps one texting number" % first["system"].replace("_", " ")
        return out
    if n_today >= int(lim.owner_texts_day):
        out.ok, out.code, out.why = False, "day", "the owner already has a text today"
        return out
    if n_week >= int(lim.owner_texts_week):
        out.ok, out.code = False, "week"
        out.why = "the owner has had %d texts in the last seven days" % n_week
        return out
    if campaign:
        clear = platform_clear([t["day"] for t in mine if t["campaign"] and t["system"] != OTHER and t["day"] < today.isoformat()], lim)
        if clear and today.isoformat() < clear:
            out.ok, out.code, out.clear_on = False, "platform", clear
            out.why = "the texting platform rests a contact %d days after %d texts in a week; it is clear on %s" % (
                lim.platform_rest_days, lim.platform_cap, date.fromisoformat(clear).strftime("%b %d").replace(" 0", " "))
            return out
    if int(lim.monthly_texts) and messages(month=today.isoformat()[:7], today=today.isoformat(),
                                           exclude_batch=exclude_batch) >= int(lim.monthly_texts):
        out.ok, out.code = False, "month"
        out.why = "this month's %d texts are used" % lim.monthly_texts
        return out
    if lim.day_capacity and messages(day=today.isoformat(), today=today.isoformat(), exclude_batch=exclude_batch) >= lim.day_capacity:
        out.ok, out.code = False, "capacity"
        out.why = "today's %d texts are used (%d a number on %d numbers)" % (lim.day_capacity, lim.texts_per_line_day, lim.text_lines)
        return out
    return out


def capacity_left(when: datetime, lim: Limits, tz: str) -> int:
    """Messages the sending numbers can still carry today, and this month."""
    today = _day(when, tz)
    left = lim.day_capacity - messages(day=today, today=today) if lim.day_capacity else 10 ** 9
    if int(lim.monthly_texts):
        left = min(left, int(lim.monthly_texts) - messages(month=today[:7], today=today))
    return max(0, left)


# ---------------------------------------------------------------------- the nightly check
def day_breaches(start: datetime, end: datetime, lim: Limits, tz: str, policy=None) -> tuple[list[dict], dict]:
    """Every limit broken between two moments (one business day), from the stored call log and the
    text ledger. Returns (breaches, totals). A breach is {"label", "n", "examples"}; an example
    carries a caller's first name or the last four digits of a number, nothing else."""
    zone = ZoneInfo(tz)
    out: list[dict] = []

    def add(label: str, n: int, examples=()) -> None:
        if n:
            out.append({"label": label, "n": int(n), "examples": list(examples)[:3]})

    dials = [e for e in activity.events(activity.DIAL, since=start, until=end) if e["data"].get("direction") != "inbound"]
    answered = [e for e in activity.events("owner.call.answered", since=start, until=end) if e["data"].get("direction") != "inbound"]

    # --- hours, on both clocks
    late, late_ex, own, own_ex = 0, [], 0, []
    for e in dials:
        local = e["when"].astimezone(zone)
        if not lim.call_hours[0] <= _mins(local) < lim.call_hours[1]:
            late += 1
            late_ex.append("%s at %s" % (e["data"].get("caller") or "a caller", _clock(local)))
            continue
        num = e["data"].get("number") or ""
        z = zone_for(num)
        if z is not None:
            there = e["when"].astimezone(z)
            if not lim.call_hours[0] <= _mins(there) < lim.call_hours[1]:
                own += 1
                own_ex.append("the number ending %s at %s its time" % (num[-4:], _clock(there)))
    add("dialed outside %s on the business clock" % _hours_text(lim.call_hours), late, late_ex)
    add("dialed outside the owner's own daytime", own, own_ex)

    # --- 25 dials per caller ID line
    per_line: Counter = Counter(e["data"].get("line") or "" for e in dials)
    per_line.pop("", None)
    over = sorted(((n, ln) for ln, n in per_line.items() if n > int(lim.calls_per_line_day)), reverse=True)
    add("a caller ID line carried more than %d dials" % lim.calls_per_line_day, len(over),
        ["the line ending %s made %d" % (ln[-4:], n) for n, ln in over])

    # --- 10 lines per caller. A dial names no caller (the dialer adds the name on the answer), so a
    # line is matched to whoever answered calls on it; with one caller that day, every line is theirs.
    line_caller: dict[str, str] = {}
    for e in answered + dials:
        who, ln = (e["data"].get("caller") or "").strip(), e["data"].get("line") or ""
        if who and ln:
            line_caller.setdefault(ln, who)
    named = {(e["data"].get("caller") or "").strip() for e in answered + dials} - {""}
    lines_by: dict[str, set] = defaultdict(set)
    for e in dials:
        ln = e["data"].get("line") or ""
        if not ln:
            continue
        who = (e["data"].get("caller") or "").strip() or line_caller.get(ln) or (next(iter(named)) if len(named) == 1 else "")
        lines_by[who or "a caller the log does not name"].add(ln)
    wide = sorted(((len(v), k) for k, v in lines_by.items() if len(v) > int(lim.lines_per_caller)), reverse=True)
    add("a caller dialed from more than %d numbers" % lim.lines_per_caller, len(wide),
        ["%s used %d" % (who, n) for n, who in wide])

    # --- one call per owner a day: the same number of an owner dialed again
    per_owner_number: Counter = Counter((e["resource"], e["data"].get("number") or "") for e in dials if e["data"].get("number"))
    again = sorted(((n, num) for (_, num), n in per_owner_number.items() if n > int(lim.owner_dials_number_day)), reverse=True)
    often = "once" if lim.owner_dials_number_day == 1 else "%d times" % lim.owner_dials_number_day
    add("an owner's number was dialed more than %s in the day" % often,
        len(again), ["the number ending %s, %d times" % (num[-4:], n) for n, num in again])

    # --- the do-not-call registry, while calls are held. A dial counts only when the flag was
    # already known at the time: a number the scrub learned about afterwards was not a mistake then.
    if scrub.settings(policy).calls == "hold":
        flagged = scrub.flagged_since({e["data"].get("number") for e in dials})
        hits = [e for e in dials if (flagged.get(e["data"].get("number") or "") or "9") <= _iso(e["when"])]
        add("dialed a number on the do-not-call registry (held until the attorney answers)", len(hits),
            ["ending %s" % n[-4:] for n in sorted({e["data"].get("number") for e in hits})])

    # --- texts, from the ledger
    day_from, day_to = start.astimezone(zone).date(), end.astimezone(zone).date()
    if day_to <= day_from:
        day_to = day_from + timedelta(days=1)
    sent = rows(since_day=day_from.isoformat(), until_day=day_to.isoformat(), statuses=("released",))
    by_owner_day: dict[tuple, set] = defaultdict(set)
    for r in sent:
        by_owner_day[(r["owner_uuid"], r["day"])].add(r["touch"] or "row-%d" % r["id"])
    twice = [(k, len(v)) for k, v in by_owner_day.items() if len(v) > int(lim.owner_texts_day)]
    add("an owner got more than %d text in a day" % lim.owner_texts_day if lim.owner_texts_day == 1
        else "an owner got more than %d texts in a day" % lim.owner_texts_day, len(twice),
        ["%d texts to one owner" % n for _, n in sorted(twice, key=lambda x: -x[1])])
    heavy = []
    for owner in {r["owner_uuid"] for r in sent if r["owner_uuid"]}:
        last = (day_to - timedelta(days=1))
        week = touches(owner, last.isoformat(), since_day=(last - timedelta(days=6)).isoformat(), released_only=True)
        n = sum(1 for t in week if t["day"] <= last.isoformat())
        if n > int(lim.owner_texts_week):
            heavy.append(n)
    add("an owner got more than %d texts in seven days" % lim.owner_texts_week, len(heavy),
        ["%d texts to one owner" % n for n in sorted(heavy, reverse=True)])
    ours = [r for r in sent if r["system"] != OTHER]
    per_day: Counter = Counter(r["day"] for r in ours)
    add("more texts went out in a day than %d numbers carry at %d each" % (lim.text_lines, lim.texts_per_line_day),
        sum(1 for n in per_day.values() if lim.day_capacity and n > lim.day_capacity),
        ["%d texts on %s" % (n, d[5:]) for d, n in per_day.items() if lim.day_capacity and n > lim.day_capacity])
    flagged = scrub.flagged_since({r["number"] for r in ours})
    bad = [r for r in ours if (flagged.get(r["number"]) or "9") <= r["at"]]
    add("texted a number on the do-not-call registry", len(bad), ["ending %s" % n[-4:] for n in sorted({r["number"] for r in bad})])
    threads: dict[tuple, set] = defaultdict(set)
    for r in rows(since_day=(day_from - timedelta(days=60)).isoformat(), until_day=day_to.isoformat(), statuses=("released",)):
        if r["system"] != OTHER and r["number"]:
            threads[(r["owner_uuid"], r["number"])].add(r["system"])
    split = [num for (_, num), systems in threads.items() if len(systems) > 1]
    add("an owner's number was texted from two texting systems", len(split), ["ending %s" % n[-4:] for n in sorted(split)])
    month = day_from.isoformat()[:7]
    month_n = messages(month=month, today=(day_to - timedelta(days=1)).isoformat())
    if int(lim.monthly_texts) and month_n > int(lim.monthly_texts):
        add("the month's texts passed %d" % lim.monthly_texts, 1, ["%d so far in %s" % (month_n, month)])

    totals = {"dials": len(dials), "owners_dialed": len({e["resource"] for e in dials}), "lines": len(per_line),
              "texts": len(ours), "owners_texted": len({r["owner_uuid"] for r in ours}), "texts_this_month": month_n,
              "other_system_texts": len(sent) - len(ours)}
    return out, totals


def run_limits(ctx) -> None:
    """The nightly check: yesterday's dials and texts against every limit. It reads the account's
    call log (the shared copy) and the autopilot's own text ledger, writes nothing to the account,
    and speaks only when a limit was broken."""
    st = ctx.stage
    lim, bad = read(ctx.policy)
    for p in bad:
        ctx.out.problem(p)
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    sync = activity.sync(ctx.crm, "calls", now=ctx.now, floor_days=int(st.get("floor_days", 45)),
                         max_calls=int(st.get("max_log_calls", 400)))
    if not sync["complete"]:
        ctx.out.problem("the call log is still catching up (read through %s); this check may be short" % (sync["through"] or "nothing yet"))
    if not zones_known():
        ctx.out.problem("the area code table did not load on this box, so dials were not checked against the owner's own clock")
    local = ctx.now.astimezone(zone)
    today0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
    # An evening run (after calling hours) checks today; a morning run checks yesterday.
    last = today0 + timedelta(days=1) if local.hour >= int(st.get("day_ends_hour", 19)) else today0
    days = max(1, int(st.get("days", 1)))
    total_n, shown_day, shown = 0, "", ([], {})
    for i in range(days, 0, -1):
        start, end = last - timedelta(days=i), last - timedelta(days=i - 1)
        found, totals = day_breaches(start, end, lim, tz, ctx.policy)
        day = start.date().isoformat()
        # What the day's check found is the autopilot's own record, written in plan mode too.
        store.kv_set("autopilot:limits:day:%s" % day, json.dumps({"breaches": {b["label"]: b["n"] for b in found}, "totals": totals}))
        total_n += sum(b["n"] for b in found)
        if i == 1:
            shown_day, shown = day, (found, totals)
    found, totals = shown
    ctx.out.found = totals.get("dials", 0) + totals.get("texts", 0)
    for b in sorted(found, key=lambda x: -x["n"]):
        ctx.out.tally[b["label"]] = b["n"]
        ctx.out.problem("%d: %s%s" % (b["n"], b["label"], (" (%s)" % ", ".join(b["examples"])) if b["examples"] else ""))
    ctx.out.tally["dials checked"] = totals.get("dials", 0)
    ctx.out.tally["texts checked"] = totals.get("texts", 0)
    ctx.out.tally["caller ID lines used"] = totals.get("lines", 0)
    ctx.out.tally["texts this month, of %d" % lim.monthly_texts] = totals.get("texts_this_month", 0)
    if totals.get("other_system_texts"):
        ctx.out.tally["texts by the other texting system (from the record counter)"] = totals["other_system_texts"]
    week = week_counts(shown_day)
    if week:
        ctx.out.tally["limits broken in the last 7 days"] = sum(week.values())
    ctx.out.acted = sum(b["n"] for b in found)
    nice = date.fromisoformat(shown_day).strftime("%b %d").replace(" 0", " ")
    what = "%d dial(s) on %d line(s) and %d text(s)" % (totals.get("dials", 0), totals.get("lines", 0), totals.get("texts", 0))
    if not ctx.out.found:
        ctx.out.head = "%s: no dials or texts to check." % nice
    elif found:
        ctx.out.head = "%s: %s. %d limit(s) were broken; each is named below." % (nice, what, len(found))
    else:
        ctx.out.head = "%s: %s. Every limit held." % (nice, what)


def week_counts(last_day: str, days: int = 7) -> dict:
    """label -> how many times it was broken over the days ending on `last_day`."""
    out: Counter = Counter()
    end = date.fromisoformat(last_day)
    for i in range(days):
        raw = store.kv_get("autopilot:limits:day:%s" % (end - timedelta(days=i)).isoformat())
        if not raw:
            continue
        try:
            for label, n in (json.loads(raw).get("breaches") or {}).items():
                out[label] += int(n)
        except (ValueError, TypeError):
            continue
    return dict(out)


def recent_lines(now: datetime | None = None, tz: str = ledger.BUSINESS_TZ) -> list[str]:
    """The last checked day's broken limits as plain lines, for the morning digest."""
    today = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz)).date()
    for back in range(0, 4):
        day = (today - timedelta(days=back)).isoformat()
        raw = store.kv_get("autopilot:limits:day:%s" % day)
        if not raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        return ["Limits %s: %d, %s" % (day[5:], n, label) for label, n in sorted((d.get("breaches") or {}).items(), key=lambda kv: -kv[1])]
    return []
