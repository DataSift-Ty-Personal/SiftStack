"""How the team writes a routed Slack post (Ty, 2026-11-15): "clean up the messaging so it's a lot
more simple and organized, almost in the way that I would write", and an issues channel at about a
third-grade reading level, so the channel the team works from is not clogged with what broke.

Two layers, in order:
  1. clean(): always, free, no network. Strips the emoji icons, the "(plan: nothing written)" tag
     and programmer text (an exception's repr, a connection pool), turns "1 lead(s)" into "1 lead"
     and "12 lead(s)" into "12 leads", names a stage in plain words, and keeps a post short.
  2. rewrite(): when AGENTS_SLACK_VOICE is "ty" (the default) and a Claude key is set, one call to
     a small model rewrites the cleaned post the way Ty writes to his own team: short, plain, the
     bad number first, the next step last. An issue is written for a third-grade reader. The model
     may only move words: every number it writes must already be in what it was given (or round
     one of them), or the cleaned post is used instead. Cached by text, capped per day, and its
     cost is booked against the task that posted.
The voice rules come from the `my-writing-style` guide (internal team register): terse, plain words,
fragments for bullets, "I think" only on a judgment call, no emoji, no dashes, no ellipses, no
headings, no greeting or sign-off.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, store

log = logging.getLogger(__name__)

# Tests swap this for a fake: (system, user) -> (text, input_tokens, output_tokens).
ASK = None
# Input and output price per million tokens, booked as an estimate (claude-api skill, 2026-11-15).
# An unknown model is priced as Opus so the books run high rather than low.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0)}
DEFAULT_PRICE = (4.0, 20.0)


def price() -> tuple[float, float]:
    return PRICES.get(config.SLACK_VOICE_MODEL, DEFAULT_PRICE)

NAMES = {
    "autopilot_digest": "Morning update", "ftm_pipeline": "County board", "top1000_refresh": "Top 1,000",
    "agency_export": "Agency file", "ftm_death_screen": "County death check", "top1000_death_screen": "Top 1,000 death check",
    "ftm_auction": "Auction calendar", "hot_auction_tasks": "Auction tasks", "conventions_check": "Account tidiness",
    # Never "Yesterday's calls" for the audit: the live test read a crash of that check as "nobody was
    # called yesterday" (2026-11-15). A name says what the check is, not what it looked at.
    "touch_audit": "Call audit", "deal_sources": "Deal sources", "outreach_limits": "Outreach limits",
    "daily_capture": "Daily numbers", "full_attempt": "Full attempts", "ftm_attempts": "County call counters",
    "top1000_attempts": "Top 1,000 call counters", "speed_to_lead": "Speed to lead", "lead_clock": "Lead clock",
    "lead_check": "Lead check", "research_queue": "Research queue", "lead_appointments": "Appointments",
    "ftm_exhausted": "Out of numbers", "ftm_retrace": "County retrace", "top1000_retrace": "Top 1,000 retrace",
    "ftm_mail": "County mail", "top1000_mail": "Top 1,000 mail", "ftm_texts": "County texts", "top1000_texts": "Top 1,000 texts",
    "ftm_registry_scrub": "County do-not-call scrub", "top1000_registry_scrub": "Top 1,000 do-not-call scrub",
    "sequence_watchdog": "Sequence check", "owner_lanes": "Owner lanes", "revocation": "Do-not-contact check",
    "kpi_alarms": "KPI alarms", "goal_sheet": "Goal sheet", "obituary_lane": "Obituary lane", "heir_lane": "Heir lane",
    "complete_records": "Incomplete records", "morning_callbacks": "Callbacks", "new_hire_week": "New hire week",
    "lead_notes": "Lead notes", "lead_encumbrances": "Encumbrances", "phone_cleanup": "Phone cleanup",
    "lost_deal_audit": "Lost deals", "neighborhood_suppression": "Neighborhood check", "tax_watch": "County tax check",
    "lead_property_check": "Property check", "closer_handoff": "Closer handoff", "primary_phone": "Primary phone",
    "account_checks": "Account checks", "business_audit": "Business audit",
    "tax_sale_results": "Tax sale results", "agency_scorecard": "Agency scorecard",
    "attempt_log": "Attempt log", "call_screen": "Call screen", "contact_rate": "Contact rate",
    "kpi_tracker": "KPI tracker", "auction_mail": "Auction mail", "probate_mail": "Probate mail",
}
# Never right after or before a digit: "at 7:45: 412 dials" lost ":45:" to this until 2026-11-16.
EMOJI_RX = re.compile(r"(?<!\d):[a-z0-9_+\-]+:(?!\d)")
PLAN_TAG_RX = re.compile(r"_?\(plan: nothing written\)_?", re.I)
TECH_RX = re.compile(r"\((?:[^()]|\([^()]*\))*?(?:HTTP|Error|Exception|Traceback|host=|port=|Errno|timed out|Timeout|"
                     r"Connection|Max retries|<http)(?:[^()]|\([^()]*\))*\)")
# "29 sale(s)", "3 touch(es)", "2 company(ies)", and "1 new lead(s)" with up to two words between
# the count and the noun.
PLURAL_RX = re.compile(r"\b(\d[\d,]*)((?:\s+[A-Za-z][A-Za-z'-]*){0,2}?)\s+([A-Za-z]+)\((s|es|ies)\)")
ERROR_CLASS_RX = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception|Exit|Interrupt):\s*")
NUM_RX = re.compile(r"\d[\d,]*(?:\.\d+)?")
BANNED = (chr(0x2014), chr(0x2013), chr(0x2026))


def stage_name(stage_id: str) -> str:
    sid = str(stage_id or "").strip().replace(" ", "_")
    if sid in NAMES:
        return NAMES[sid]
    words = sid.replace("_", " ").split()
    words = ["County" if w == "ftm" else "Top 1,000" if w == "top1000" else w for w in words]
    out = " ".join(words)
    return out[:1].upper() + out[1:] if out else "The team"


def _plural(m: re.Match) -> str:
    n, mid, noun, suffix = m.group(1), m.group(2), m.group(3), m.group(4)
    if n == "1":
        word = noun
    elif suffix == "ies":
        word = (noun[:-1] if noun.endswith("y") else noun) + "ies"
    else:
        word = noun + suffix
    return "%s%s %s" % (n, mid, word)


def clean(text: str, max_lines: int | None = None) -> str:
    """The free cleanup every routed post gets: no emoji icons, no plan tag, no programmer text,
    plurals said once, short lines."""
    t = EMOJI_RX.sub("", str(text or ""))
    t = PLAN_TAG_RX.sub("", t)
    t = TECH_RX.sub("", t)
    t = re.sub(r"[ \t]+([;,.])", r"\1", t)   # what the error text left behind: "did not answer ; 60 records"
    t = ERROR_CLASS_RX.sub("", t)        # "RuntimeError: the CRM answered 502" reads "the CRM answered 502"
    t = PLURAL_RX.sub(_plural, t)
    t = re.sub(r"y\(ies\)", "ies", t).replace("(es)", "es").replace("(s)", "s").replace("*", "")
    for ch in BANNED:
        t = t.replace(ch, ", " if ch == chr(0x2014) else "-" if ch == chr(0x2013) else "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip(" ;") for ln in t.splitlines()]
    lines = [ln for ln in lines if ln]
    if max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)


# ---------------------------------------------------------------- the rewrite
VOICE = """You rewrite one Slack post from an AI operations team to the owner's real estate team. Write it the way
the owner writes to his own team: short, plain, direct. Numbers first. No fluff.

Rules:
- Use only what the post says. Never add a number, name, date, reason, cause or step that is not in it.
  Keep every street, person, date and dollar figure exactly as written. You may round a big number with "about".
- No greeting, no sign-off, no emoji, no headings, no tables, no bold.
- Never use em dashes, en dashes or ellipses. Use commas and periods.
- Lead with the bad number when something is off. Use "I think" only for a judgment call the post makes.
- Write out what a code means in plain words (a stage name, a list name). Never write code, file names or error text.
- Output only the rewritten post, nothing before or after it."""

SHAPES = {
    "update": "Shape: one headline line under 12 words, then at most 3 short lines that start with '- '. "
              "Sentence fragments are fine.",
    "alert": "Shape: one or two short lines. Start with what someone needs to do now and keep the address in it.",
    "report": "Shape: one headline line under 12 words that says what the report is about, then at most 6 short lines "
              "that start with '- '. Keep every street, date and dollar figure exactly as written. Sentence fragments "
              "are fine.",
    "digest": "Shape: first line 'Morning update', with the date only if the post gives one. Then one line that starts "
              "'Yesterday:' holding only what the post says happened yesterday or in the last 24 hours (never what is "
              "coming up, such as sales or auctions ahead). Then at most 6 "
              "short lines that start with '- ': first what needs a person (a lead waiting, a batch waiting on someone), "
              "then what is coming up, then the counts. Put counts of the same kind on one line (two Ready to Call "
              "lists become one line). Write a list's name in plain words: FTM means county, and a number like 02 in a "
              "list's name is dropped. Leave out a line that only says nothing went wrong, and the system's own "
              "housekeeping (its request budget, its waits). If the post says where the "
              "problems are, keep that as the last line.",
    # The first version asked for four fixed lines (what broke, what it means, what happens next, what
    # to do), and the model filled any line the post did not answer with a guess. Now a line exists only
    # when the post says it.
    "issue": "This post goes to the issues channel. Write it for a third-grade reader. One short sentence per line, at "
             "most four lines, no labels and no headings. First say which check it was and what broke, in plain words. "
             "Then, only when the post says it: what "
             "that means for the team, what happens next, and what a person should do. Never guess a consequence, a "
             "cause or a next step the post does not state. When the post names something a person should do or "
             "watch for, end with that. Only when it names nothing like that and says it will be tried again, runs "
             "again or fixes itself, end with 'Nothing for you to do.' Every sentence under 10 words. Everyday words "
             "only. No code, no error text, no file names, no field names.",
}

# The second look, by the same small model: a rewrite that says anything the post did not is never
# posted (the live test of 2026-11-15 turned a crashed audit into "nobody was called yesterday").
JUDGE = """You check one rewrite of a Slack post against the original post. Answer with one word: PASS or FAIL.
FAIL when the rewrite says anything the original does not: a new fact, number, name, cause, consequence, date or
step, or a claim that something did or did not happen that the original does not state.
FAIL as well when the rewrite drops something the original tells a person to do or watch for.
FAIL when it calls a thing by another name that changes what it is (records are not listings, a stage is not a
person), when it adds "now", "just" or "new" to something the original states as a standing fact (that reads as a
change that just happened), or when it adds a label the original does not have, such as "Morning update".
PASS when it only says the same things in simpler or fewer words. Dropping other detail is fine. Writing "today"
for something the post reports as happening now is fine. Turning a stage's name into plain words is fine. Ending
with "Nothing for you to do." is fine when the original says it will be tried again, runs again or fixes itself,
and names nothing a person should do or watch for."""
CHECK = None                     # tests: (source, written) -> bool


def _numbers(text: str) -> list[float]:
    out = []
    for m in NUM_RX.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _numbers_hold(source: str, written: str) -> bool:
    """Every number in what the model wrote is one from the post, or a rounding of one (within 2%)."""
    have = _numbers(source)
    for n in _numbers(written):
        if not any(abs(n - s) <= max(0.02 * abs(s), 0.5) for s in have):
            return False
    return True


def acceptable(source: str, written: str, kind: str) -> bool:
    w = (written or "").strip()
    if not w or len(w) > (700 if kind == "issue" else 1400):
        return False
    if any(ch in w for ch in BANNED) or EMOJI_RX.search(w):
        return False
    if kind == "issue" and re.search(r"Exception|Traceback|HTTP|host=|port=|\(\)|_[a-z]+_", w):
        return False
    return _numbers_hold(source, w)


def _ask_claude(system: str, user: str) -> tuple[str, int, int]:
    """One rewrite. Claude Haiku 5.5 thinks by default, and the first live run spent its whole reply
    on thinking (2026-11-15: stop_reason max_tokens, one thinking block, no text). Moving words around
    needs no reasoning, so thinking is off (Haiku 5.5 accepts that at effort high or below) and effort
    is low. A model that refuses "disabled" (Opus 5.5, Sonnet 5.5) is asked once more without it, with
    room to think."""
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    base = dict(model=config.SLACK_VOICE_MODEL, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}])
    try:
        msg = client.messages.create(max_tokens=800, thinking={"type": "disabled"}, **base)
    except anthropic.BadRequestError:
        msg = client.messages.create(max_tokens=4000, **base)
    u = getattr(msg, "usage", None)
    tin, tout = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    if getattr(msg, "stop_reason", "") in ("refusal", "max_tokens"):
        return "", tin, tout          # a cut-off or refused rewrite is never posted; the cleaned post is
    text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text").strip()
    return text, tin, tout


def _today() -> str:
    tz = ZoneInfo(getattr(config, "BUSINESS_TZ", "") or "America/New_York")
    return datetime.now(tz).strftime("%Y-%m-%d")


def rewrite(text: str, kind: str, task_id: int | None = None) -> str:
    """The cleaned post in the team's voice, or the cleaned post itself when the rewrite is off, over
    its daily cap, unreachable, or wrote something the post did not say."""
    base = clean(text)
    if not base or config.SLACK_VOICE != "ty" or not (ASK or config.ANTHROPIC_API_KEY):
        return base
    key = "voice:%s" % hashlib.sha256(("%s\n%s" % (kind, base)).encode("utf-8")).hexdigest()[:24]
    kept = store.kv_get(key)
    if kept:
        return kept
    count_key = "voice:count:%s" % _today()
    n = int(store.kv_get(count_key) or 0)
    if n >= config.SLACK_VOICE_DAILY:
        return base
    try:
        written, tin, tout = (ASK or _ask_claude)(VOICE + "\n\n" + SHAPES.get(kind, SHAPES["update"]), base)
    except Exception as exc:  # noqa: BLE001  (a rewrite that fails never stops a post)
        log.warning("voice rewrite failed (%s); the cleaned post is used", str(exc)[:120])
        return base
    store.kv_set(count_key, str(n + 1))
    _book(task_id, tin, tout)
    written = (written or "").strip().strip('"').strip()
    if kind == "issue":
        # One sentence a line: the model ran them together in one paragraph on the first live test.
        written = "\n".join(ln.strip() for ln in re.split(r"(?<=[.!?])\s+(?=[A-Z])", written) if ln.strip())
        if written.count("\n") > 4:
            log.info("voice rewrite refused for an issue (more than five lines); the cleaned post is used")
            return base
    if not acceptable(base, written, kind):
        log.info("voice rewrite refused for %s (a number or a banned mark it was not given); the cleaned post is used", kind)
        return base
    if not _faithful(base, written, task_id):
        log.info("voice rewrite refused for %s (it said something the post did not); the cleaned post is used", kind)
        return base
    store.kv_set(key, written)
    return written


def _book(task_id, tin: int, tout: int) -> None:
    if task_id:
        p_in, p_out = price()
        store.add_spend(int(task_id), (tin * p_in + tout * p_out) / 1e6, "slack voice (estimate)")


def _faithful(source: str, written: str, task_id=None) -> bool:
    """The second look. Fails closed: a check that cannot be made is a rewrite that is not used."""
    if CHECK:
        return bool(CHECK(source, written))
    try:
        verdict, tin, tout = (ASK or _ask_claude)(JUDGE, "ORIGINAL:\n%s\n\nREWRITE:\n%s" % (source, written))
    except Exception as exc:  # noqa: BLE001
        log.warning("voice check failed (%s); the cleaned post is used", str(exc)[:120])
        return False
    _book(task_id, tin, tout)
    return (verdict or "").strip().upper().startswith("PASS")


# ---------------------------------------------------------------- the posts, by kind
def update_text(stage_id: str, head: str, lines: list, task_id=None) -> str:
    body = ["%s: %s" % (stage_name(stage_id), head or "")] + [str(x) for x in (lines or [])[:4]]
    return rewrite("\n".join(body), "update", task_id)


def report_text(stage_id: str, head: str, lines: list, task_id=None) -> str:
    """A report stage's daily or weekly report: the auction calendar, the day's numbers, the goals."""
    body = ["%s: %s" % (stage_name(stage_id), head or "")] + [str(x) for x in (lines or [])[:6]]
    return rewrite("\n".join(body), "report", task_id)


def digest_text(head: str, lines: list, task_id=None, problems: int = 0) -> str:
    """The morning digest. Where its problems went is said by the code, never left for the rewrite to
    claim (the second look refused a digest that said so on its own, 2026-11-15)."""
    body = [head or ""] + [str(x) for x in lines or []]
    if problems and config.SLACK_ISSUES_CHANNEL and config.SLACK_ISSUES_CHANNEL != config.SLACK_PIPELINE_CHANNEL:
        body.append("%d problem%s %s in the issues channel." % (problems, "" if problems == 1 else "s",
                                                                 "is" if problems == 1 else "are"))
    return rewrite("\n".join(body), "digest", task_id)


def alert_text(stage_id: str, problem: str, task_id=None) -> str:
    return rewrite("%s: %s" % (stage_name(stage_id), problem), "alert", task_id)


def issue_text(stage_id: str, problem: str, task_id=None) -> str:
    return rewrite("%s: %s" % (stage_name(stage_id), problem), "issue", task_id)


def problem_key(stage_id: str, problem: str) -> str:
    """One problem's identity across runs: counts change by the hour, the problem does not."""
    shape = re.sub(r"\d+", "#", clean(problem)).lower()
    return "slack:issue:%s:%s" % (stage_id, hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16])


def first_today(stage_id: str, problem: str) -> bool:
    """True the first time a problem is seen on a business day (and records it): an issue is said once
    a day while it lasts, never every 30 minutes."""
    key = problem_key(stage_id, problem)
    today = _today()
    if store.kv_get(key) == today:
        return False
    store.kv_set(key, today)
    return True


def once(stage_id: str, text: str) -> bool:
    """True the first time this exact text is seen for a stage, ever (and records it). For alerts: two
    appointments differ only by a house number or a time, which the digit-blind problem key would
    merge into one, and an appointment re-read tomorrow must not be announced again."""
    key = "slack:once:%s:%s" % (stage_id, hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:20])
    if store.kv_get(key):
        return False
    store.kv_set(key, store.now())
    return True


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
