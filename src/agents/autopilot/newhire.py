"""A new hire's first week: every answered call graded each evening, the notes to the owner first.

Ty, 2026-11-15: "New-hire first week: every call graded daily, with the coaching notes coming to you
before the caller." The two lessons it automates:
  * "For the first week in any new role (cold calling, lead management or closing), review every
    call and give feedback every single day."
  * "Send the automated coaching notes to the owner first to touch up and forward, feed the owner's
    edits back into the CLAUDE.md, and once the output is consistent send it to callers automatically."

The weekly coaching picks choose two best and two worst calls; a first week is too short for a sample.
This stage takes EVERY answered call the new hire made that day (from the shared log copy, matched on
the caller's name or user id), transcribes and grades each with the call coaching engine on the
rubric of the person's role, and writes the day's notes: per call the score and up to three things to
keep and three to change, each quoting the call, then the day as a whole.

Why it is built this way:
  * THE OWNER READS FIRST. The notes are staged as a batch (kind newhire) and the team channel is
    told they are waiting. Nothing reaches the new hire until a person named in AGENTS_RELEASERS
    releases the batch (release batch N, then confirm N). This stage never posts to anyone, and plan
    mode stages nothing.
  * THE OWNER'S EDITS ARE KEPT. A reply "edit N: <your version>" replaces the draft of batch N, and
    both texts are stored side by side (edits()), so the difference can later be fed back into the
    coaching instructions. Nothing here changes an instructions file.
  * PAID ONCE, INSIDE TWO CAPS. Every result also goes into the call coaching workflow's own store
    (wf_coach_grades), so a call graded here is not paid for again when the weekly picks choose it,
    and the reverse. Each paid step asks this stage's dollars a day (daily_usd) first, then the
    autopilot's and the brain's caps; calls a day (max_calls_per_day) caps each new hire's day.
  * A DAY IS WRITTEN UP ONCE, WHEN IT IS OVER. A run before the calling hours end grades what is in
    and waits. The notes are staged after the end of the day's hours, and only once the log copy has
    caught up past them, so an afternoon of calls is never left out of a day's notes.
  * NO TRANSCRIPT, NO GRADE (the engine's rule). A voicemail or a wrong number is counted, never
    scored. A quote the call does not hold word for word is left out of the notes and named for the
    owner, because a new hire told they said something they never said stops trusting the notes.
  * PRIVACY. Transcripts stay in the brain's database. The notes and the Slack lines carry no phone
    number and no homeowner name: the owner of record's names (one read per owner) and a name said
    right after "is this" or a title become [name], a number becomes [number]. A street stays. The
    recording link is never written anywhere.

It reads the call log copy and owner names and writes nothing to DataSift: no homeowner is contacted,
so the gate has nothing to judge. The one person it ever reaches is the new hire, on release.
"""
from __future__ import annotations

import difflib
import html
import json
import logging
import re
import urllib.parse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, slack, store
from . import activity, budget, kpi, ledger

log = logging.getLogger(__name__)

KIND = "newhire"
POLICY = None                    # the selftest's playbook; None reads team/autopilot.yaml
SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_newhire_calls (
    person TEXT, day TEXT, call_id TEXT, at TEXT, seconds INTEGER, owner TEXT, status TEXT, why TEXT,
    usd REAL DEFAULT 0, tried INTEGER DEFAULT 0, graded_at TEXT, PRIMARY KEY (person, day, call_id));
CREATE TABLE IF NOT EXISTS ap_newhire_days (
    person TEXT, day TEXT, live INTEGER, status TEXT, batch_id INTEGER DEFAULT 0, graded INTEGER DEFAULT 0,
    at TEXT, PRIMARY KEY (person, day, live));
CREATE TABLE IF NOT EXISTS ap_newhire_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, stage TEXT, batch_id INTEGER DEFAULT 0, person TEXT,
    day TEXT, role TEXT, file TEXT DEFAULT '', draft TEXT, edited TEXT DEFAULT '', edited_by TEXT DEFAULT '',
    edited_at TEXT DEFAULT '', sent TEXT DEFAULT '', sent_to TEXT DEFAULT '', sent_at TEXT DEFAULT '');
"""
DEFAULTS = {"first_week_days": 5, "max_calls_per_day": 25, "daily_usd": 10.0, "min_seconds": 15,
            "catch_up_days": 1, "points": 3, "max_log_calls": 200}
LIMITS = {"first_week_days": (1, 20), "max_calls_per_day": (1, 200), "min_seconds": (0, 600),
          "catch_up_days": (0, 5), "points": (1, 3), "max_log_calls": (1, 2000)}
ROLES = {"caller": "cold_call", "cold caller": "cold_call", "cold call": "cold_call",
         "lead manager": "lead_management", "lead management": "lead_management",
         "closer": "closing", "closing": "closing"}
ROLE_WORDS = {"cold_call": "caller", "lead_management": "lead manager", "closing": "closer"}
PERSON_KEYS = {"name", "role", "start", "slack_user", "uuid"}
SLACK_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")
WHY = "a new hire's first week, where every answered call is graded"
# What the engine's triage calls a call it does not grade, in the words the notes use.
UNGRADED_WORDS = {"voicemail": ("voicemail", "voicemails"), "wrong_number": ("wrong number", "wrong numbers"),
                  "no_contact": ("call that reached nobody", "calls that reached nobody"),
                  "dead_air": ("call with no conversation", "calls with no conversation")}
NO_GRADE = ("not_gradeable", "not_scored", "no_transcript")    # final: nothing to grade, nothing to retry
RETRY = ("left", "ungraded", "error", "stopped")               # not final: a later run tries again
CHUNK = 3500                     # one Slack message holds 3,900 characters (slack_app.post cuts there)


# ---------------------------------------------------------------------- settings and people
def settings(st) -> dict:
    def g(k):
        return st.get(k, DEFAULTS[k])
    return {"days": int(g("first_week_days")), "max_calls": int(g("max_calls_per_day")),
            "daily_usd": float(g("daily_usd")), "min_seconds": int(g("min_seconds")), "catch_up": int(g("catch_up_days")),
            "points": int(g("points")), "log_calls": int(g("max_log_calls")), "inbound": bool(st.get("include_inbound", False)),
            "probe": bool(st.get("probe", True)), "sync": bool(st.get("sync", True))}


def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _role(v) -> str | None:
    return ROLES.get(" ".join(str(v or "").replace("_", " ").split()).lower())


def _start(v) -> date | None:
    """A start day as YAML hands it over: a date (an unquoted 2026-11-16) or the same as text."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v or "").strip())
    except ValueError:
        return None


def people(st) -> list[dict]:
    """The new hires the stage block lists, each with the rubric their role is graded on. A person the
    block cannot read is left out here and refused by block_problems when the playbook loads."""
    out = []
    for raw in st.get("people") or []:
        if not isinstance(raw, dict):
            continue
        name = " ".join(str(raw.get("name") or "").split())
        pipe, start = _role(raw.get("role")), _start(raw.get("start"))
        if name and pipe and start:
            out.append({"name": name, "first": name.split()[0], "pipeline": pipe, "role": ROLE_WORDS[pipe], "start": start,
                        "slack_user": str(raw.get("slack_user") or "").strip(), "uuid": str(raw.get("uuid") or "").strip()})
    return out


def block_problems(st) -> list[str]:
    """A mistyped block is refused when the playbook loads (policy.parse), never read as a default: a
    wrong start day would grade the wrong week, and a wrong role the wrong rubric."""
    p = []
    raw_people = st.get("people", [])
    if not isinstance(raw_people, list):
        return ["stage %s: people must be a list of new hires, each with a name, a role and a start day" % st.id]
    seen = set()
    for i, raw in enumerate(raw_people, 1):
        if not isinstance(raw, dict):
            p.append("stage %s: new hire %d must be a mapping with name, role and start" % (st.id, i))
            continue
        name = " ".join(str(raw.get("name") or "").split())
        who = name or "new hire %d" % i
        if not name:
            p.append("stage %s: new hire %d needs a name, written the way the call log names the caller" % (st.id, i))
        elif name.casefold() in seen:
            p.append("stage %s: %s is listed twice" % (st.id, name))
        seen.add(name.casefold())
        if _role(raw.get("role")) is None:
            p.append("stage %s: %s has role %r (use caller, lead manager or closer)" % (st.id, who, raw.get("role")))
        if _start(raw.get("start")) is None:
            p.append("stage %s: %s needs a start day written YYYY-MM-DD" % (st.id, who))
        su = str(raw.get("slack_user") or "").strip()
        if su and not SLACK_ID.match(su):
            p.append("stage %s: %s has slack_user %r, which is not a Slack member id (U or W and capitals)" % (st.id, who, su))
        extra = sorted(set(raw) - PERSON_KEYS)
        if extra:
            p.append("stage %s: %s has %s, which a new hire does not take (%s)" % (st.id, who, extra, ", ".join(sorted(PERSON_KEYS))))
    for key, (lo, hi) in LIMITS.items():
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            p.append("stage %s: %s must be a whole number from %d to %d" % (st.id, key, lo, hi))
    for key in ("include_inbound", "probe", "sync"):
        v = st.get(key)
        if v is not None and not isinstance(v, bool):
            p.append("stage %s: %s must be true or false" % (st.id, key))
    return p


def in_first_week(pol, today: date) -> set[str]:
    """The callers a newhire stage grades every call of this week, by the name the call log writes.
    For the coaching picks: a person graded here must not also get a daily pack of four picked calls,
    or the owner reads two sets of notes on the same calls."""
    out: set[str] = set()
    for st in (getattr(pol, "stages", None) or {}).values():
        if st.kind != KIND or st.mode == "off":
            continue
        cfg = settings(st)
        out |= {p["name"] for p in people(st) if 1 <= day_number(p["start"], today, pol.days) <= cfg["days"]}
    return out


# ---------------------------------------------------------------------- days
def day_number(start: date, day: date, days) -> int:
    """Which business day of the new hire's time this is: 1 on the first business day on or after the
    start, 0 before the start or on a day off."""
    if day < start or day.weekday() not in days:
        return 0
    n, d = 0, start
    while d <= day:
        n += d.weekday() in days
        d += timedelta(days=1)
    return n


def recent_days(today: date, back: int, days) -> list[date]:
    """The business days a run looks at, oldest first: today when it is one, and `back` business days
    before it, so an evening the box was down is caught up the next evening."""
    out = [today] if today.weekday() in days else []
    d = today
    for _ in range(14):
        if sum(1 for x in out if x != today) >= back:
            break
        d -= timedelta(days=1)
        if d.weekday() in days:
            out.append(d)
    return sorted(out)


def _day_label(d: date) -> str:
    return "%s %s %d" % (d.strftime("%a"), d.strftime("%b"), d.day)


def _clock(when: datetime, tz: str) -> str:
    return when.astimezone(ZoneInfo(tz)).strftime("%I:%M%p").lstrip("0").lower()


def _hour_text(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return "%d%s%s" % (h % 12 or 12, (":%02d" % m) if m else "", "pm" if h >= 12 else "am")


def _length(seconds) -> str:
    return "%d min" % max(1, round((seconds or 0) / 60.0))


def _fmt(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return str(int(round(f))) if abs(f - round(f)) < 0.05 else "%.1f" % f


def _over(day: date, local: datetime, hours) -> bool:
    if day < local.date():
        return True
    return day == local.date() and local.hour * 60 + local.minute >= int(hours[1])


def _caught_up(day: date, tz: str, hours, cfg: dict) -> bool:
    """Has the log copy read past the end of this day's calling hours? Written up any sooner, a day's
    notes would miss the calls the copy had not read yet, and a staged day is never opened again."""
    if not cfg["sync"]:
        return True
    through = activity.synced_through("calls")
    return through is not None and through >= kpi.day_start(day, tz) + timedelta(minutes=int(hours[1]))


# ---------------------------------------------------------------------- the calls, from the log copy
def theirs(data: dict, person: dict) -> bool:
    """A call is the new hire's when the log names them exactly, or carries their user id. Never a
    first-name guess: another caller's calls in a new hire's notes would coach the wrong person."""
    if person.get("uuid") and str(data.get("caller_uuid") or "") == person["uuid"]:
        return True
    return bool(person.get("name")) and _norm(data.get("caller")) == _norm(person["name"])


def calls_of(person: dict, day: date, tz: str, cfg: dict) -> tuple[list[dict], Counter]:
    """The new hire's answered calls of one day with a recording, oldest first, and what was passed over."""
    start = kpi.day_start(day, tz)
    out, skipped, seen = [], Counter(), set()
    for e in activity.events(("owner.call.answered", "owner.call.received"), since=start, until=start + timedelta(days=1)):
        d = e["data"]
        if not theirs(d, person):
            continue
        if (e["event_type"] == "owner.call.received" or d.get("direction") == "inbound") and not cfg["inbound"]:
            continue
        cid = str(d.get("call") or e["id"])
        if cid in seen:
            continue
        seen.add(cid)
        secs = int(d.get("duration") or 0)
        if secs < cfg["min_seconds"]:
            skipped["shorter than %d seconds (a greeting or a hang-up)" % cfg["min_seconds"]] += 1
            continue
        if not d.get("recording"):
            skipped["no recording in the call log"] += 1
            continue
        out.append({"call": cid, "caller": str(d.get("caller") or "").strip(), "owner": e["resource"], "when": e["when"],
                    "at": e["when"].isoformat(timespec="seconds"), "seconds": secs, "recording": str(d["recording"])})
    return out, skipped


def near_names(person: dict, day: date, tz: str) -> list[str]:
    """Callers in the log that day who share the new hire's first name but not the whole name: the
    likely reason a listed new hire seems to have made no calls."""
    start = kpi.day_start(day, tz)
    first, out = _norm(person["name"]).split(" ")[0], set()
    for e in activity.events(("owner.call.answered", "owner.call.made"), since=start, until=start + timedelta(days=1)):
        who = str(e["data"].get("caller") or "").strip()
        if who and _norm(who) != _norm(person["name"]) and _norm(who).split(" ")[0] == first:
            out.add(who)
    return sorted(out)


# ---------------------------------------------------------------------- this stage's own tables
def ensure() -> None:
    kpi.ensure("ap_newhire", SCHEMA)


def _done(person: str, day: str, live: bool) -> bool:
    """A live run counts only live marks: a day a plan run looked at was never graded. A plan run
    counts both, so it says what live would do."""
    with store._LOCK, store._db() as c:
        return bool(c.execute("SELECT 1 FROM ap_newhire_days WHERE person=? AND day=? AND live>=?",
                              (person, day, 1 if live else 0)).fetchone())


def _set_day(person: str, day: str, live: bool, status: str, batch_id: int = 0, graded: int = 0) -> None:
    with store._LOCK, store._db() as c:
        c.execute("INSERT OR REPLACE INTO ap_newhire_days (person, day, live, status, batch_id, graded, at) VALUES (?,?,?,?,?,?,?)",
                  (person, day, 1 if live else 0, status, int(batch_id or 0), int(graded or 0), store.now()))


def _rows(person: str, day: str) -> dict:
    with store._LOCK, store._db() as c:
        return {r["call_id"]: dict(r) for r in c.execute("SELECT * FROM ap_newhire_calls WHERE person=? AND day=?",
                                                         (person, day)).fetchall()}


def _keep(person: str, day: str, c: dict, status: str, why: str, usd: float = 0.0, tried: int = 0) -> None:
    with store._LOCK, store._db() as db:
        db.execute("INSERT OR REPLACE INTO ap_newhire_calls (person, day, call_id, at, seconds, owner, status, why, usd, tried, "
                   "graded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (person, day, c["call"], c["at"], c["seconds"], c["owner"], status, why or "", round(float(usd or 0), 4),
                    int(tried), store.now()))


def _note(notes_id: int) -> dict | None:
    with store._LOCK, store._db() as c:
        r = c.execute("SELECT * FROM ap_newhire_notes WHERE id=?", (int(notes_id or 0),)).fetchone()
    return dict(r) if r else None


def _save_edit(notes_id: int, text: str, user: str) -> None:
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_newhire_notes SET edited=?, edited_by=?, edited_at=? WHERE id=?",
                  (text, user or "", store.now(), int(notes_id)))


def edits(since_day: str = "") -> list[dict]:
    """Every day of notes the owner changed before release: the draft, the owner's version and the
    lines that differ, oldest first. For a later pass that feeds the owner's changes back into the
    coaching instructions (the lesson's loop); nothing here changes an instructions file."""
    ensure()
    with store._LOCK, store._db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM ap_newhire_notes WHERE edited<>'' AND day>=? ORDER BY id",
                                           (since_day or "",)).fetchall()]
    out = []
    for r in rows:
        diff = [ln for ln in difflib.unified_diff((r["draft"] or "").splitlines(), (r["edited"] or "").splitlines(),
                                                  lineterm="", n=0)
                if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        out.append({"batch": r["batch_id"], "person": r["person"], "day": r["day"], "role": r["role"], "draft": r["draft"],
                    "edited": r["edited"], "edited_by": r["edited_by"], "edited_at": r["edited_at"], "changed": diff})
    return out


# ---------------------------------------------------------------------- privacy
PHONE_RX = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d)")
SHORT_PHONE_RX = re.compile(r"(?<!\d)\d{3}[\s.-]\d{4}(?!\d)")
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ASKED_RX = re.compile(r"(?i:\b(is this|am i speaking with|am i speaking to|speaking with|talking with|talking to))"
                      r"\s+([A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?)")
TITLE_RX = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Mister)\.?\s+[A-Z][a-z'-]+(?:\s+[A-Z][a-z'-]+)?")
NAMES_RX = re.compile(r"\[name\](?:\s+\[name\])+")


def _name_rx(token: str):
    """A name as a capitalized word, whatever case the record holds it in ("PAT" matches "Pat"), so the
    word "pat" in a sentence is left alone."""
    return re.compile(r"\b%s(?i:%s)\b" % (re.escape(token[0].upper()), re.escape(token[1:])))


def scrub(text: str, names=(), keep=()) -> str:
    """No phone number, no email and no homeowner name in anything a person reads. `names` are the owner
    of record's name tokens; a name said right after "is this" or a title goes too; `keep` (the new
    hire's own name) stays. A street is not a name and stays."""
    t = EMAIL_RX.sub("[email]", text or "")
    t = PHONE_RX.sub("[number]", t)
    t = SHORT_PHONE_RX.sub("[number]", t)
    keep_set = {_norm(k) for k in keep if k}
    for tok in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
        if _norm(tok) not in keep_set:
            t = _name_rx(tok).sub("[name]", t)

    def asked(m) -> str:
        return m.group(0) if _norm(m.group(2).split()[0]) in keep_set else "%s [name]" % m.group(1)
    t = ASKED_RX.sub(asked, t)
    t = TITLE_RX.sub("[name]", t)
    return NAMES_RX.sub("[name]", t)


def _owner_names(ctx, owner: str, cache: dict) -> list[str] | None:
    """The owner of record's name tokens, one read per owner per run. None when the read failed."""
    if owner in cache:
        return cache[owner]
    try:
        o = ctx.crm.owner(owner) or {}
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (an owner that cannot be read is said, not guessed)
        log.warning("new hire notes: owner %s not read (%s)", owner, str(exc)[:80])
        cache[owner] = None
        return None
    toks = [t for k in ("first_name", "last_name") for t in re.split(r"[\s,/&-]+", str(o.get(k) or "")) if len(t) >= 2]
    cache[owner] = toks
    return toks


# ---------------------------------------------------------------------- reading a grade
HEAD_KEEP = re.compile(r"^top\s*3\s+strengths?\b", re.I)
HEAD_CHANGE = re.compile(r"^top\s*3\s+improvement", re.I)
HEAD_FIX = re.compile(r"^one\s+fix\s*:\s*", re.I)
ITEM_RX = re.compile(r"^\d+[.)]\s+")
CAPS_HEAD = re.compile(r"^[A-Z][A-Z0-9 /&()'-]{2,}(?::|$)")
QUOTE_RX = re.compile(r'"([^"]{3,})"')
INSTEAD_RX = re.compile(r"\b(?:use|say|try)\s+instead\s*:?|\binstead\s*:", re.I)
CRIT_RX = re.compile(r"^\[?(\d+\.\d+)\]?\s*")
LABEL_RX = re.compile(r"^(?:they said|what happened|you said)\s*:?\s*", re.I)


def sections(report: str) -> tuple[list[str], list[str]]:
    """The report's TOP 3 STRENGTHS and TOP 3 IMPROVEMENT AREAS items (all three rubrics write both
    on a full call), plus a short call's ONE FIX as a change. A continuation line ("Use instead: ...")
    joins its item; the next heading, table or fence ends a section."""
    keeps: list[str] = []
    changes: list[str] = []
    cur, fence = None, False
    for raw in (report or "").splitlines():
        s = raw.strip()
        if s.startswith("```"):
            fence, cur = not fence, None
            continue
        if fence:
            continue
        if HEAD_KEEP.match(s):
            cur = keeps
            continue
        if HEAD_CHANGE.match(s):
            cur = changes
            continue
        m = HEAD_FIX.match(s)
        if m:
            if s[m.end():].strip():
                changes.append(s[m.end():].strip())
            cur = None
            continue
        if cur is None or not s:
            continue
        m = ITEM_RX.match(s)
        if m:
            cur.append(s[m.end():])
        elif s.startswith("|") or CAPS_HEAD.match(s):
            cur = None
        elif cur:
            cur[-1] = cur[-1] + " " + s
    return keeps, changes


def item_parts(text: str) -> dict:
    """One item as {crit, quote, instead, why}, whichever of the three rubrics' shapes it is in."""
    t = (text or "").replace(chr(0x201C), '"').replace(chr(0x201D), '"').replace(chr(0x2018), "'").replace(chr(0x2019), "'").strip()
    crit = ""
    m = CRIT_RX.match(t)
    if m:
        crit, t = m.group(1), t[m.end():]
    t = LABEL_RX.sub("", t, count=1)
    m = INSTEAD_RX.search(t)
    said, rest = (t[:m.start()], t[m.end():]) if m else (t, "")
    q = QUOTE_RX.search(said)
    quote = q.group(1).strip() if q else ""
    why = (said[q.end():] if q else said).strip(" :.;,-")
    instead = ""
    if rest:
        qi = QUOTE_RX.search(rest)
        instead = qi.group(1).strip() if qi else rest.strip(" :.;,-")
        after = rest[qi.end():].strip(" :.;,-") if qi else ""
        why = ("%s. %s" % (why, after) if why else after) if after else why
    return {"crit": crit, "quote": quote, "instead": instead, "why": why}


def _sentence(s: str) -> str:
    s = (s or "").strip().strip(" :;,-")
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


def _keep_line(part: dict) -> str:
    if part["quote"]:
        return '"%s"%s' % (part["quote"], (": " + _sentence(part["why"])) if part["why"] else "")
    return _sentence(part["why"])


def _change_line(part: dict) -> str:
    bits = []
    if part["quote"]:
        bits.append('You said "%s".' % part["quote"])
    if part["instead"]:
        bits.append('Try instead: "%s"' % part["instead"])
    if part["why"]:
        bits.append(_sentence(part["why"]))
    return " ".join(bits)


def _grade_mod():
    """The rubric grader (src/call_coaching/grade.py), imported the way the call coaching workflow does."""
    from ..workflows import call_coaching
    return call_coaching._engine()[0]


def _in_call(quote: str, transcript: str) -> bool:
    """The grader's own verify pass, on one quote."""
    return not _grade_mod().unverified_quotes('"%s"' % quote.replace('"', "'"), transcript or "")


def _criteria(report: str) -> dict:
    sc = next((b for b in _grade_mod().json_blocks(report or "") if "call_type" in b), None) or {}
    return {str(k): float(v) for k, v in (sc.get("criteria") or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _score_text(r: dict) -> str:
    if str(r.get("call_type") or "") == "short":
        return "a short call, scored on its own scale"
    if r.get("total") is None:
        return "no total came back"
    if str(r.get("band") or "") == "FAIL":
        return "%s, but it broke an automatic-fail rule (%s)" % (_fmt(r["total"]), r.get("auto_fail") or "fail")
    return "%s (%s)" % (_fmt(r["total"]), r.get("band") or _grade_mod().band(r["total"]))


# ---------------------------------------------------------------------- the notes
def write_notes(person: dict, day: date, n: int, cfg: dict, entries: list[dict], owners: dict, tz: str) -> dict:
    """The day's notes. Returns {"draft": what the new hire gets, "owner": lines only the owner reads,
    "graded", "average"}. `entries` hold each call of the day with its final status and stored grade."""
    first, points = person["first"], cfg["points"]
    keep_names = person["name"].split()
    graded = [e for e in entries if e["final"] == "graded"]
    out = ["%s, here are the notes on your calls of %s, day %d of your first week." % (first, _day_label(day), n), ""]
    owner_lines: list[str] = []
    totals, crit = [], defaultdict(list)
    for i, e in enumerate(graded, 1):
        r, c = e["stored"], e["call"]
        names = owners.get(c["owner"]) or []
        head = "Call %d, %s, %s: %s" % (i, _clock(c["when"], tz), _length(c["seconds"]), _score_text(r))
        keeps_raw, changes_raw = sections(r.get("report") or "")
        if not keeps_raw and not changes_raw and isinstance(r.get("note"), dict) and r["note"].get("said"):
            n_ = r["note"]
            line = '"%s"%s' % (n_["said"], (": " + n_["why"]) if n_.get("why") else "")
            (keeps_raw if n_.get("focus") == "reinforce" else changes_raw).append(
                line + (' Use instead: "%s"' % n_["instead"] if n_.get("instead") else ""))
        keeps, changes = [], []
        for raws, into, render in ((keeps_raw, keeps, _keep_line), (changes_raw, changes, _change_line)):
            for raw in raws:
                part = item_parts(raw)
                if part["quote"] and not _in_call(part["quote"], r.get("transcript") or ""):
                    owner_lines.append('Left out of call %d: a point quoting "%s", which the call does not hold word for word.'
                                       % (i, scrub(part["quote"], names, keep_names)))
                    continue
                line = render(part)
                if line and len(into) < points:
                    into.append(line)
        block = [head]
        if keeps:
            block += ["Keep doing:"] + ["- " + x for x in keeps]
        if changes:
            block += ["Change:"] + ["- " + x for x in changes]
        if not keeps and not changes:
            block.append("The grade came back with no point to quote for this call.")
        out += [scrub(x, names, keep_names) for x in block] + [""]
        for prob in r.get("problems") or []:
            if "not in the transcript" not in str(prob):
                owner_lines.append("Check call %d: %s." % (i, scrub(str(prob), names, keep_names).rstrip(".")))
        if r.get("total") is not None:
            totals.append((float(r["total"]), c))
        for k, v in _criteria(r.get("report") or "").items():
            crit[k].append(v)
    out.append("Your day")
    avg = sum(t for t, _ in totals) / len(totals) if totals else None
    if avg is not None:
        out.append("%d call%s graded, average %s (%s)." % (len(graded), "" if len(graded) == 1 else "s", _fmt(avg),
                                                           _grade_mod().band(avg)))
    else:
        out.append("%d call%s graded." % (len(graded), "" if len(graded) == 1 else "s"))
    if len(totals) >= 2:
        hi, lo = max(totals, key=lambda x: (x[0], -x[1]["when"].timestamp())), min(totals, key=lambda x: (x[0], x[1]["when"]))
        if hi[0] != lo[0]:
            out.append("Best %s at %s, lowest %s at %s." % (_fmt(hi[0]), _clock(hi[1]["when"], tz), _fmt(lo[0]),
                                                            _clock(lo[1]["when"], tz)))
    kinds = Counter(str((e["stored"] or {}).get("call_type") or "") for e in entries if e["final"] in NO_GRADE)
    if kinds:
        bits = []
        for k, v in kinds.most_common():
            one, many = UNGRADED_WORDS.get(k, ("call with nothing to grade", "calls with nothing to grade"))
            bits.append("%d %s" % (v, one if v == 1 else many))
        total = sum(kinds.values())
        out.append("%d more call%s %s not graded: %s." % (total, "" if total == 1 else "s", "was" if total == 1 else "were",
                                                         " and ".join(bits)))
    if crit:
        names_of = _criterion_names(person["pipeline"])
        avgs = {k: sum(v) / len(v) for k, v in crit.items()}
        order = sorted(avgs, key=lambda k: [int(x) for x in k.split(".")])
        best = max(order, key=lambda k: avgs[k])
        worst = min(order, key=lambda k: avgs[k])
        if avgs[best] > avgs[worst]:
            out.append("Keep doing: %s, your strongest point today (%s of 5)." % (
                ("%s %s" % (best, names_of.get(best, ""))).strip(), _fmt(avgs[best])))
            out.append("Focus tomorrow: %s, your weakest point today (%s of 5)." % (
                ("%s %s" % (worst, names_of.get(worst, ""))).strip(), _fmt(avgs[worst])))
    for e in entries:
        if e["final"] in RETRY:
            owner_lines.append("Not graded: the %s call (%s)." % (_clock(e["call"]["when"], tz), scrub(e["why"] or e["final"])))
    clean = _grade_mod().clean
    draft = clean("\n".join(" ".join(x.split()) if x else "" for x in out).strip())
    return {"draft": draft, "owner": [clean(x) for x in owner_lines], "graded": len(graded), "average": avg}


_NAMES: dict = {}


def _criterion_names(pipeline: str) -> dict:
    if pipeline not in _NAMES:
        try:
            g = _grade_mod()
            _NAMES[pipeline] = g.criterion_names(g.rubric(pipeline))
        except Exception:  # noqa: BLE001  (a rubric that is not on the box: the numbers still read)
            _NAMES[pipeline] = {}
    return _NAMES[pipeline]


def owner_part(person: dict, batch_id: int, usd: float, lines: list[str]) -> str:
    """The lines at the top of the notes file that only the owner reads; they are never sent."""
    first = person["first"]
    out = ["For you first: %s does not see this part." % first,
           ("Batch %d. Nothing goes to %s until you release it: release batch %d, then confirm %d. To change the notes, "
            "reply in the thread with edit %d: and your version of them." % (batch_id, first, batch_id, batch_id, batch_id)),
           "Paid for these calls: $%.2f." % usd]
    return "\n".join(out + list(lines))


def _slug(text: str) -> str:
    return "_".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())[:40] or "new_hire"


# ---------------------------------------------------------------------- money
class _Spend:
    """What call coaching's grade_one asks before each paid step and tells after it, answered by this
    stage: its own dollars a day first, then the autopilot's and the brain's caps (ctx.budget). Each
    charge goes in the ledger under this stage, so the next run's cap sees today's spend."""

    def __init__(self, ctx, cap: float, person: str):
        self.ctx, self.cap, self.person = ctx, float(cap), person

    def allows(self, usd: float) -> str:
        d = ledger.day(self.ctx.now, self.ctx.policy.tz)
        if ledger.spent(d, self.ctx.stage.id) + usd > self.cap + 1e-9:
            return "about $%.2f more would pass this stage's $%.2f a day (daily_usd)" % (usd, self.cap)
        ok, why = self.ctx.budget(usd)
        return "" if ok else "about $%.2f more would pass the %s" % (usd, why)

    def charge(self, usd: float, what: str) -> None:
        if usd <= 0:
            return
        self.ctx.settle(0.0, usd, what)
        ledger.event(self.ctx.run_id, self.ctx.stage.id, "", "coach_grade", {"person": self.person, "what": what},
                     usd=usd, now=self.ctx.now, tz=self.ctx.policy.tz)


def _reusable(prior: dict | None, pipeline: str) -> bool:
    """A stored result counts for this new hire when nothing in it depends on the rubric (a voicemail),
    or it was graded on the rubric of their role."""
    if not prior:
        return False
    if prior.get("status") in ("not_gradeable", "no_transcript"):
        return True
    return prior.get("status") in ("graded", "not_scored") and prior.get("pipeline") == pipeline


def _pick(c: dict, person: dict, day: str) -> dict:
    return {"call": c["call"], "caller": c["caller"] or person["name"], "kind": "", "at": c["at"], "seconds": c["seconds"],
            "recording": c["recording"], "why": WHY, "period": "newhire:%s:%s" % (day, person["name"])}


def _cc():
    from ..workflows import call_coaching
    return call_coaching


def _stored(call_id: str) -> dict:
    """Call coaching's stored result for one call with its JSON columns read back: the store keeps the
    note and the grader's problems as JSON text, and a string read as a list is a list of letters."""
    r = dict(_cc().stored(call_id) or {})
    for key, empty in (("note", {}), ("problems", [])):
        v = r.get(key)
        if isinstance(v, str):
            try:
                v = json.loads(v or "null")
            except ValueError:
                v = None
        r[key] = v if isinstance(v, type(empty)) else empty
    return r


# ---------------------------------------------------------------------- one day
def _plan_day(ctx, run: dict, person: dict, day: str, calls: list[dict], cfg: dict) -> dict:
    """What a live run would do with this day, paying for nothing: the calls it would send, what that
    would cost at most, and any recording host that would stop it (one probe per host, first bytes)."""
    cc = _cc()
    mine = _rows(person["name"], day)
    tried = sum(1 for r in mine.values() if r["tried"])
    room = cfg["daily_usd"] - ledger.spent(ledger.day(ctx.now, ctx.policy.tz), ctx.stage.id) - run["would_usd"]
    out = {"would": 0, "usd": 0.0, "reused": 0, "capped": 0, "stopped": 0}
    for c in calls:
        row = mine.get(c["call"])
        if row and row["status"] not in RETRY:
            continue
        prior = cc.stored(c["call"])
        if _reusable(prior, person["pipeline"]):
            out["reused"] += 1
            continue
        host = urllib.parse.urlsplit(c["recording"]).hostname or ""
        if cfg["probe"] and host not in run["probed"]:
            run["probed"][host] = cc._fetch(c["recording"], True, run["cc"]["max_bytes"])
            p = run["probed"][host]
            if not p.get("ok") and p.get("code") == "login":
                run["blocked"][host] = p.get("why") or "the recording link needs a login the brain does not have"
        if host in run["blocked"]:
            out["stopped"] += 1
            continue
        est = cc.estimate(_pick(c, person, day), run["cc"])
        cost = est["grade"] if (prior or {}).get("transcript") else est["total"]
        if tried + out["would"] >= cfg["max_calls"] or out["usd"] + cost > room + 1e-9:
            out["capped"] += 1
            continue
        out["would"] += 1
        out["usd"] += cost
    return out


def _live_day(ctx, run: dict, person: dict, day: str, calls: list[dict], cfg: dict) -> None:
    """Grade every call of the day that has no final result yet, inside the caps."""
    cc = _cc()
    tz = ctx.policy.tz
    mine = _rows(person["name"], day)
    tried = sum(1 for r in mine.values() if r["tried"])
    spend = _Spend(ctx, cfg["daily_usd"], person["name"])
    roles = dict(run["cc"])
    for c in calls:
        row = mine.get(c["call"])
        if row and row["status"] not in RETRY:
            continue
        prior = cc.stored(c["call"])
        if _reusable(prior, person["pipeline"]):
            _keep(person["name"], day, c, "reused", "graded before by call coaching")
            continue
        if tried >= cfg["max_calls"]:
            _keep(person["name"], day, c, "left", "past the day's limit of %d graded calls (max_calls_per_day)" % cfg["max_calls"])
            continue
        host = urllib.parse.urlsplit(c["recording"]).hostname or ""
        if host in run["blocked"]:
            _keep(person["name"], day, c, "stopped", run["blocked"][host])
            continue
        if c["seconds"] > run["cc"]["max_minutes"] * 60:
            _keep(person["name"], day, c, "stopped", "longer than %d minutes, too long to send in one piece" % run["cc"]["max_minutes"])
            continue
        pick = _pick(c, person, day)
        roles["roles"] = {pick["caller"].lower(): person["pipeline"]}       # the role picks the rubric, not the triage
        r = cc.grade_one(pick, cc._row(pick), spend, roles, tz, prior)
        if r["status"] == "stopped" and "login" in str(r.get("why") or ""):
            run["blocked"][host] = r["why"]
        if r["status"] != "left":
            cc.keep(r, ctx.task_id)
        # A call counts toward the day's cap once it was sent to be graded; one the caps or a login
        # wall stopped before any money moved does not, so it can still be graded later that day.
        sent = 0 if r["status"] in ("left", "stopped") else 1
        tried += sent
        _keep(person["name"], day, c, r["status"], r.get("why") or "", r.get("usd") or 0.0, tried=sent)
        run["graded"] += r["status"] == "graded"


def _final(row: dict | None, stored: dict | None) -> tuple[str, str]:
    """A call's last word: the grade it carries, or why it has none yet."""
    if not row:
        return "left", "not reached by a run yet"
    if row["status"] == "reused":
        return str((stored or {}).get("status") or "left"), str((stored or {}).get("why") or "")
    return row["status"], row["why"] or ""


def _stage_notes(ctx, run: dict, person: dict, d: date, n: int, cfg: dict, entries: list[dict]) -> dict:
    """Write the day's notes and stage them for the owner. Nothing is sent from here."""
    owners: dict = {}
    for e in entries:
        if e["final"] == "graded":
            names = _owner_names(ctx, e["call"]["owner"], run["names"])
            if names is None:
                ctx.out.problem("could not read an owner's name to take it out of the notes; read them for names before "
                                "you release them")
            owners[e["call"]["owner"]] = names or []
    notes = write_notes(person, d, n, cfg, entries, owners, ctx.policy.tz)
    day = d.isoformat()
    usd = sum(float(r["usd"] or 0) for r in _rows(person["name"], day).values())
    name = "notes_%s_%s.txt" % (_slug(person["name"]), day)
    with store._LOCK, store._db() as c:
        nid = c.execute("INSERT INTO ap_newhire_notes (created, stage, person, day, role, file, draft) VALUES (?,?,?,?,?,?,?)",
                        (store.now(), ctx.stage.id, person["name"], day, person["role"], name, notes["draft"])).lastrowid
    bid = ledger.batch_create(ctx.stage.id, KIND, [{"person": person["name"], "day": day, "notes_id": nid, "file": name,
                                                    "stage_task": int(ctx.task_id or 0), "graded": notes["graded"],
                                                    "record_uuid": ""}], 0.0)
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_newhire_notes SET batch_id=? WHERE id=?", (bid, nid))
    folder = Path(run["run_dir"]) / "files"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(owner_part(person, bid, usd, notes["owner"]) + "\n\nThe notes %s gets:\n\n%s\n" % (
        person["first"], notes["draft"]), encoding="utf-8")
    ctx.out.run_dir = run["run_dir"]
    _set_day(person["name"], day, True, "staged", bid, notes["graded"])
    ledger.event(ctx.run_id, ctx.stage.id, "", "newhire_notes", {"person": person["name"], "day": day, "batch": bid,
                                                                 "graded": notes["graded"]}, now=ctx.now, tz=ctx.policy.tz)
    return {"batch": bid, "person": person, "day": d, "n": n, "graded": notes["graded"], "average": notes["average"], "file": name}


def _work_day(ctx, run: dict, person: dict, d: date, n: int, cfg: dict) -> None:
    tz, hours, day = ctx.policy.tz, ctx.policy.hours, d.isoformat()
    calls, skipped = calls_of(person, d, tz, cfg)
    run["found"] += len(calls)
    for why, k in skipped.items():
        ctx.out.tally["answered calls passed over: %s" % why] += k
    over = _over(d, run["local"], hours)
    ready = over and _caught_up(d, tz, hours, cfg)
    label = "%s's calls for %s (day %d of %d)" % (person["first"], _day_label(d), n, cfg["days"])
    if not calls:
        near = near_names(person, d, tz)
        if near:
            ctx.out.problem("no call names %s as the caller, but calls name %s; write the name under people exactly as the "
                            "call log does" % (person["name"], " and ".join(near)))
        if over:
            # Said after the days that were graded: a quiet day is news, the waiting notes come first.
            run["quiet"].append("%s made no answered call with a recording on %s (day %d of %d)." % (
                person["first"], _day_label(d), n, cfg["days"]))
            if ready and not near:
                _set_day(person["name"], day, ctx.live, "no_calls")
        return
    if not run["pay"]:
        p = _plan_day(ctx, run, person, day, calls, cfg)
        run["would"] += p["would"]
        run["would_usd"] += p["usd"]
        extra = []
        if p["reused"]:
            extra.append("%d graded before" % p["reused"])
        if p["capped"]:
            extra.append("%d past the day's caps" % p["capped"])
        if p["stopped"]:
            extra.append("%d behind a recording login" % p["stopped"])
        tail = (" (%s)" % ", ".join(extra)) if extra else ""
        if over:
            run["items"].append("Would grade up to %d of %s%s, at most about $%.2f, then stage the notes for you first." % (
                p["would"], label, tail, p["usd"]))
            if ready and not ctx.live:
                _set_day(person["name"], day, False, "planned")
        else:
            run["items"].append("Would grade up to %d of %s so far%s, at most about $%.2f; the notes would be staged after %s." % (
                p["would"], label, tail, p["usd"], _hour_text(hours[1])))
        return
    _live_day(ctx, run, person, day, calls, cfg)
    mine = _rows(person["name"], day)
    entries = []
    for c in calls:
        stored = _stored(c["call"])
        final, why = _final(mine.get(c["call"]), stored)
        entries.append({"call": c, "final": final, "why": why, "stored": stored})
    graded = sum(1 for e in entries if e["final"] == "graded")
    if not over:
        run["items"].append("Graded %d of %s so far; the notes are staged after the calling day ends at %s." % (
            graded, label, _hour_text(hours[1])))
        return
    if not ready:
        run["items"].append("%s are graded; the notes wait for the call log copy to hold every call of the day." % label)
        ctx.out.problem("the call log copy is still catching up, so a day's notes wait until it holds every call of the day")
        return
    if graded:
        run["staged"].append(_stage_notes(ctx, run, person, d, n, cfg, entries))
        return
    pending = [e for e in entries if e["final"] in RETRY]
    if pending:
        top = Counter(e["why"] for e in pending).most_common(1)[0][0]
        ctx.out.problem("%s's calls for %s were not graded (%s); the day's notes wait for the next run" % (
            person["first"], _day_label(d), top))
        return
    run["items"].append("%s held no conversation to grade (%d answered call%s, voicemails and the like)." % (
        label[0].upper() + label[1:], len(entries), "" if len(entries) == 1 else "s"))
    _set_day(person["name"], day, True, "nothing_graded")


# ---------------------------------------------------------------------- the stage
def _asked(ctx) -> bool:
    if not ctx.task_id:
        return False
    return str((store.task(ctx.task_id) or {}).get("source") or "").startswith("slack")


def _sync(ctx, cfg: dict) -> None:
    try:
        r = activity.sync(ctx.crm, "calls", now=ctx.now, max_calls=cfg["log_calls"])
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (a stale copy still holds the morning's calls; the day waits)
        ctx.out.problem("could not refresh the call log before grading (%s); calls since the last refresh wait for the "
                        "next run" % str(exc)[:80])
        return
    if not r.get("complete"):
        ctx.out.problem("the call log copy is still catching up, so a day's notes wait until it holds every call of the day")


def run_newhire(ctx) -> None:
    ensure()
    st, tz = ctx.stage, ctx.policy.tz
    cfg = settings(st)
    folks = people(st)
    asked = _asked(ctx)
    nobody = "autopilot:newhire:nobody:%s" % st.id
    if not folks:
        if asked or not store.kv_get(nobody):
            ctx.out.head = ("Nobody is listed as a new hire, so no calls are graded. Add a person under people in this stage "
                            "(the name the call log writes, the role and the start day) to grade their first week.")
            store.kv_set(nobody, "said")
        return
    store.kv_set(nobody, "")                 # someone is listed: an empty list later is said again, once
    local = ctx.now.astimezone(ZoneInfo(tz))
    work = []
    for p in folks:
        for d in recent_days(local.date(), cfg["catch_up"], ctx.policy.days):
            n = day_number(p["start"], d, ctx.policy.days)
            if 1 <= n <= cfg["days"] and not _done(p["name"], d.isoformat(), ctx.live):
                work.append((p, d, n))
    if not work:
        if asked:
            ctx.out.head = "Nobody listed is in their first week today with a day left to grade, so no calls are graded."
        return
    if cfg["sync"]:
        _sync(ctx, cfg)
    cc = _cc()
    missing = cc._keys_missing() if ctx.live else ""
    if missing:
        ctx.out.problem("%s; nothing was paid and no notes were staged" % missing)
    run = {"pay": ctx.live and not missing, "cc": cc.settings(), "blocked": {}, "probed": {}, "names": {}, "local": local,
           "staged": [], "items": [], "quiet": [], "found": 0, "graded": 0, "would": 0, "would_usd": 0.0,
           "run_dir": str(Path(config.DATA_DIR) / "newhire" / ("%s-%s" % (st.id, ctx.task_id or ctx.run_id)))}
    for p, d, n in work:
        _work_day(ctx, run, p, d, n, cfg)
    for host, why in sorted(run["blocked"].items()):
        ctx.out.problem("the recording links on %s need a login the brain does not have, so calls recorded there are not "
                        "graded (%s)" % (host, why))
    ctx.out.found = run["found"]
    ctx.out.acted = run["graded"] if run["pay"] else run["would"]
    ctx.out.would_usd = round(run["would_usd"], 2)
    _say(ctx, run)


def _say(ctx, run: dict) -> None:
    """The short line to the team channel: whose notes wait, the batch to release, where to read them."""
    from .. import release as release_mod
    staged, items = run["staged"], list(run["items"]) + list(run["quiet"])
    if not staged:
        if items:
            ctx.out.head, rest = items[0], items[1:]
            for x in rest:
                ctx.out.line(x)
        return
    ctx.out.batch = staged[-1]["batch"]

    def what(s) -> str:
        avg = (", average %s" % _fmt(s["average"])) if s["average"] is not None else ""
        return "%d call%s graded%s, batch %d" % (s["graded"], "" if s["graded"] == 1 else "s", avg, s["batch"])
    if len(staged) == 1:
        s = staged[0]
        ctx.out.head = "%s's notes for %s (day %d of %d) are waiting for you: %s." % (
            s["person"]["first"], _day_label(s["day"]), s["n"], settings(ctx.stage)["days"], what(s))
    else:
        ctx.out.head = "Notes for %d new hire days are waiting for you: batches %s." % (
            len(staged), " and ".join(str(s["batch"]) for s in staged))
        for s in staged:
            ctx.out.line("%s, %s (day %d): %s." % (s["person"]["first"], _day_label(s["day"]), s["n"], what(s)))
    for s in staged:
        url = slack.file_url(ctx.task_id, s["file"]) if ctx.task_id else ""
        where = url or "the brain, under newhire/%s" % Path(run["run_dir"]).name
        ctx.out.line("Read %s's notes for %s: %s" % (s["person"]["first"], _day_label(s["day"]), where))
    b = staged[0]["batch"]
    ctx.out.line("Nothing goes to %s until you release it. To change the notes, reply here with edit %d: and your version, "
                 "then release batch %d and confirm %d." % (
                     staged[0]["person"]["first"] if len(staged) == 1 else "a new hire", b, b, b))
    for x in items:
        ctx.out.line(x)
    if not release_mod.releasers():
        ctx.out.problem("nobody is named to release batches in Slack (AGENTS_RELEASERS), so new hire notes can only be "
                        "sent from the command line")


# ---------------------------------------------------------------------- the owner's edit
EDIT_RX = re.compile(r"^\s*edits?\s+(?:batch\s+)?#?(\d+)\s*:\s*(\S[\s\S]*)$", re.I)


def parse_edit(text: str) -> tuple[int, str] | None:
    """(batch id, the owner's version of the notes) for "edit 12: <the notes>", else None. The colon is
    required, so "edit 12 later" is ordinary talk. Slack's &amp; and the like are turned back."""
    m = EDIT_RX.match(text or "")
    if not m:
        return None
    body = html.unescape(m.group(2)).strip()
    return (int(m.group(1)), body) if body else None


def slack_edit(parsed: tuple[int, str], user: str, channel: str, thread: str) -> dict:
    """Keep the owner's version of one day's notes, from a reply in Slack. For intake to call right
    after the release command (an edit, like a release, is read by a pattern and never by a model).
    Only a named releaser may change what a new hire gets."""
    from .. import release as release_mod, slack_app
    ensure()
    bid, text = parsed

    def say(t: str) -> None:
        slack_app.post(channel, t, thread_ts=thread)
    if user not in release_mod.releasers():
        say("Only a person named as a releaser can change a new hire's notes. The owner adds a person by their Slack id "
            "(AGENTS_RELEASERS).")
        return {"edit": "refused", "why": "not a releaser"}
    b = ledger.batch(bid)
    if not b or b.get("kind") != KIND:
        say("There is no batch %d of new hire notes." % bid)
        return {"edit": "refused", "why": "no such batch"}
    if b["status"] != "staged":
        say("Batch %d is %s, so its notes can no longer be changed." % (bid, b["status"]))
        return {"edit": "refused", "why": "not staged"}
    note = _note(int(((b.get("items") or [{}])[0]).get("notes_id") or 0))
    if not note:
        say("The notes of batch %d are not on the brain any more." % bid)
        return {"edit": "refused", "why": "no notes"}
    _save_edit(note["id"], text, user)
    say("Saved your version of batch %d (%d lines). %s gets it instead of the draft when you release batch %d and confirm %d."
        % (bid, len(text.splitlines()), note["person"].split()[0], bid, bid))
    return {"edit": "saved", "batch": bid}


def _thread_edit(b: dict) -> tuple[str, str] | None:
    """The owner's edit as intake handles it today: a reply in the thread of the waiting line becomes an
    ask on that thread, which keeps its text. The newest "edit N:" for this batch from a releaser wins."""
    from .. import release as release_mod
    allowed = release_mod.releasers()
    item = (b.get("items") or [{}])[0]
    t = store.task(int(item.get("stage_task") or 0)) if item.get("stage_task") else None
    if not allowed or not t or not (t.get("slack_channel") and t.get("slack_thread")):
        return None
    found = None
    for task in store.on_slack_thread(t["slack_channel"], t["slack_thread"]):
        if task.get("workflow") != "ask":
            continue
        p = store.payload_of(task)
        ed = parse_edit(str(p.get("text") or ""))
        if ed and ed[0] == int(b["id"]) and str(p.get("user") or "") in allowed:
            found = (ed[1], str(p.get("user")))
    return found


# ---------------------------------------------------------------------- the release
def _listed(pol, stage_id: str, name: str) -> dict | None:
    try:
        st = pol.stage(stage_id)
    except Exception:  # noqa: BLE001  (no playbook, or the stage is gone)
        return None
    return next((p for p in people(st) if _norm(p["name"]) == _norm(name)), None)


def _policy():
    if POLICY is not None:
        return POLICY
    from . import policy
    try:
        return policy.load()
    except Exception:  # noqa: BLE001  (a playbook that does not load: nobody is listed, nothing is sent)
        return None


def _team_channel() -> str:
    from .. import slack_app, team
    if config.SLACK_CHANNEL:
        return config.SLACK_CHANNEL
    try:
        return slack_app.home_channel(None, team.load())
    except Exception:  # noqa: BLE001
        return ""


def chunks(text: str, size: int = CHUNK) -> list[str]:
    """The notes in Slack-sized messages, cut between calls (a blank line), never inside a sentence."""
    pieces = []
    for block in (text or "").split("\n\n"):
        while len(block) > size:
            cut = block.rfind("\n", 0, size)
            cut = cut if cut > 0 else size
            pieces.append(block[:cut])
            block = block[cut:].lstrip("\n")
        pieces.append(block)
    out, cur = [], ""
    for pc in pieces:
        if cur and len(cur) + 2 + len(pc) > size:
            out.append(cur)
            cur = pc
        else:
            cur = (cur + "\n\n" + pc) if cur else pc
    if cur:
        out.append(cur)
    return out or [""]


def _slack_safe(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def release(batch_id: int, *, commit: bool = False, pol=None, **kw) -> dict:
    """Send one staged day of notes to the new hire: the owner's version when there is one, else the
    draft. Dry unless commit. agents/release.py calls this after a named releaser confirms in Slack;
    the command line can too (cli.py autopilot release N --commit). A person no longer listed in the
    playbook, or a Slack that does not take the message, sends nothing and leaves the batch staged."""
    from .. import slack_app
    ensure()
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch, not new hire notes" % (batch_id, b["kind"]))
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    item = (b.get("items") or [{}])[0]
    note = _note(int(item.get("notes_id") or 0))
    base = {"batch": batch_id, "sent": 0, "of": 1}
    if not note:
        return dict(base, problems=["the notes of batch %d are not on the brain any more, so nothing was sent" % batch_id])
    who = _listed(pol or _policy(), b["stage"], note["person"])
    if who is None:
        return dict(base, problems=["%s is no longer listed as a new hire in the playbook, so the notes were not sent"
                                    % note["person"]])
    edit, by = note.get("edited") or "", note.get("edited_by") or ""
    if not edit:
        found = _thread_edit(b)
        if found:
            edit, by = found
            if commit:
                _save_edit(note["id"], edit, by)
    text = scrub(edit or note["draft"], keep=who["name"].split())
    where = ("%s by direct message" % who["first"]) if who["slack_user"] else "the team channel"
    if not commit:
        return dict(base, dry=True, would_send_to=where, edited=bool(edit), characters=len(text))
    if not config.SLACK_BOT_TOKEN:
        return dict(base, problems=["the Slack app is not connected (SLACK_BOT_TOKEN), so the notes cannot be sent; batch "
                                    "%d is still staged" % batch_id])
    channel = who["slack_user"] or _team_channel()
    if not channel:
        return dict(base, problems=["no team channel is set (AGENTS_SLACK_CHANNEL) and %s has no Slack id, so the notes "
                                    "have nowhere to go; batch %d is still staged" % (who["first"], batch_id)])
    parts = chunks(text)
    employee = _employee(b["stage"], pol or _policy())
    first = slack_app.post(channel, _slack_safe(parts[0]), employee=employee)
    if not first:
        return dict(base, problems=["Slack did not take the notes for %s, so nothing was sent; batch %d is still staged"
                                    % (who["first"], batch_id)])
    went = 1 + sum(1 for part in parts[1:] if slack_app.post(channel, _slack_safe(part), thread_ts=first, employee=employee))
    problems = [] if went == len(parts) else ["%d of the %d messages of the notes reached Slack" % (went, len(parts))]
    with store._LOCK, store._db() as c:
        c.execute("UPDATE ap_newhire_notes SET sent=?, sent_to=?, sent_at=? WHERE id=?", (text, where, store.now(), note["id"]))
    out = dict(base, sent=1, edited=bool(edit), problems=problems,
               results={"%s to %s" % ("message" if went == 1 else "messages", where): went})
    ledger.batch_set(batch_id, "released", out)
    ledger.event("", b["stage"], "", "newhire_sent", {"person": note["person"], "day": note["day"], "edited": bool(edit),
                                                      "to": where})
    return out


def _employee(stage_id: str, pol):
    """The stage's owner on the team (Quinn), so the notes arrive under a name the new hire knows."""
    try:
        from .. import team
        return team.load().employees.get(pol.stage(stage_id).owner)
    except Exception:  # noqa: BLE001  (no roster: the app's own name is fine)
        return None


def release_note(b: dict) -> str:
    """The words agents/release.py adds when a person asks to release a batch of new hire notes: who
    gets them and how, where to read them first, and whether the owner's version replaces the draft."""
    ensure()
    item = (b.get("items") or [{}])[0]
    note = _note(int(item.get("notes_id") or 0))
    if not note:
        return "Its notes are not on the brain any more, so a release sends nothing."
    who = _listed(_policy(), b.get("stage") or "", note["person"])
    if who is None:
        return "%s is no longer listed as a new hire, so a release sends nothing." % note["person"]
    day = _day_label(date.fromisoformat(note["day"]))
    if who["slack_user"]:
        bits = ["It sends %s the notes on %s by direct message." % (who["first"], day)]
    else:
        bits = ["It posts %s's notes for %s in the team channel, since no Slack id is set for %s." % (
            who["first"], day, who["first"])]
    url = slack.file_url(int(item["stage_task"]), item["file"]) if item.get("stage_task") and item.get("file") else ""
    if url:
        bits.append("Read them first: %s" % url)
    if note.get("edited") or _thread_edit(b):
        bits.append("Your version replaces the draft.")
    else:
        bits.append("To change them first, reply with edit %d: and your version." % int(b["id"]))
    return " ".join(bits)
