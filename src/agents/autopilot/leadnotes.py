"""Lead notes from the call recording (build plan 2.9, which waits on decision D16; Ty, 2026-11-15:
"Lead notes from the call recording: the four pillars and the intake answers (asking price,
condition, occupancy, mortgage) written onto the record, so nobody asks the seller twice at the
handoff").

Why it exists. At every handoff (lead management, acquisitions, transactions, dispo) the seller must
never be made to repeat an answer: the next person confirms what the last one captured (C8.21). That
only works when the answers are on the record, and today they live in a caller's memory. The lessons
add two rules this stage keeps: lead notes follow one trained format every time, tied to the four
pillars of motivation (C9.17: reason for selling, timeline and urgency, property condition, price
expectations), and before a hot lead that is ready for an offer moves to Send to Acquisitions, every
note is already on the record. D16 (open; recommended): about 20 core answers come from the call, not
the 75-field human form. The ones that change the offer go to custom fields, the rest to the note.

Each run:
  1. reads the brain's copy of the call log (activity.py) for answered calls with a recording, at
     least `min_seconds` long, since its last run. A call counts when the owner's house has a lead
     status, or the owner has had any answered call of `prospect_seconds` or more (a real
     conversation on a prospect);
  2. finds the house. An owner with several houses is held unless exactly one of them is a lead: a
     note on the wrong house is worse than no note. A company or trust is left alone (people only),
     and the gate runs on the record;
  3. live: opens the recording from the link the log carries (never with a login or a guess),
     transcribes it with the coaching transcriber (OpenRouter audio, about $0.003 a minute) and asks
     Claude Haiku 5.5, thinking off, to fill one fixed schema: the four pillars and the intake
     answers the lessons teach;
  4. keeps only the answers whose quote is in the call. Every value must carry a quote copied from
     the transcript, and the code checks that the quote is there (speaker labels, delivery notes and
     punctuation aside), that it is at least two words long, and that every number in the value is
     in its quote. Anything else is Unknown. Nothing is inferred: a model that fills a gap from what
     is usual for a house or a seller writes a fact nobody said onto the record;
  5. writes ONE note per call in ONE format (the date and who called; Reason, Timeline, Condition and
     Price; each intake answer known, with its quote; what the call did not cover), and the answers
     whose custom fields exist (`fields`: Timeline exists today, the others are named once as a
     problem until they are created). A value a person typed in a field is never overwritten, and
     every field write keeps the value it replaced, for undo;
  6. when a record's status moves into a hot or acquisitions stage (`handoff_statuses`), writes one
     "What we know" note merging every call note on file, newest answer first, each with its date,
     so acquisitions starts from the record instead of from the seller.

Money: transcription and the notes are paid, inside the stage's own daily cap and the autopilot's,
with per-run caps on transcriptions, model calls and record reads. A call is paid for once: its
transcript is kept, so a note that failed is retried without transcribing again, and a transcript the
coaching workflow already paid for is used as it is. Plan mode reads the log and the records and
says how many calls it would transcribe and what that would cost; it opens no recording and spends
nothing. Its marks are its own, so a later live run still does every call a plan run only counted.

Privacy: audio never touches the disk; transcripts stay in the brain's data folder (config.DATA_DIR)
and are deleted after `keep_days`. A Slack line carries counts and street addresses only, never a
quote, a phone number or the recording link. A note cannot be taken off a record through the API, so
undo puts the fields back and leaves the notes.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
import model_registry
from . import activity, budget, lanes, ledger
from .stages import addr

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

# Stand-ins the selftest sets, so no test can reach a dialer, OpenRouter or Claude. None = the real one.
FETCH = None        # (url, max_bytes) -> a dict shaped like call_coaching.transcribe.fetch_recording's
TRANSCRIBE = None   # (audio, fmt, caller) -> (transcript, usage)
ASK = None          # (system, user, model, max_tokens) -> (reply text, usage, stop reason)

UNKNOWN = "Unknown"
# The four pillars of motivation (C9.17), always printed, in this order.
PILLARS = (
    ("reason", "Reason", "why the seller wants to sell"),
    ("timeline", "Timeline", "when they want or need to sell, and how urgent it is"),
    ("condition", "Condition", 'the house in one phrase: "excellent", "needs a little updating", "needs a lot of work", '
                               "or the seller's own phrase"),
    ("price", "Price", "what the seller hopes to get, in their own words"),
)
# The intake answers the lessons teach, printed when the call gave them.
INTAKE = (
    ("asking_price", "Asking price", "the price the seller named, if they named one"),
    ("firm", "Firm on price", "whether the seller is firm on that price"),
    ("other_offers", "Other offers", "other offers the seller has had"),
    ("listed", "Listed with an agent lately", "whether it was listed with an agent in the last 6 months"),
    ("under_contract", "Under contract", "whether it is under contract with anyone now"),
    ("occupancy", "Occupancy", "who lives there: the owner, a tenant (with or without a lease) or nobody"),
    ("last_update", "Last major update", "the year of the last major update, or never"),
    ("roof", "Roof", "the roof's age, and whether it is insurable"),
    ("soft_floors", "Soft floors", "any soft floors"),
    ("water_damage", "Water damage or flooding", "any water damage or flooding"),
    ("electrical", "Electrical panel", "the type of electrical panel"),
    ("hvac", "Heating and air", "the age of the heating and air"),
    ("vehicles", "Vehicles on the property", "vehicles left on the property"),
    ("mortgage_balance", "Mortgage balance", "what is owed on the mortgage"),
    ("months_behind", "Months behind", "how many months behind on the payments"),
    ("back_taxes", "Back taxes", "back property taxes owed"),
    ("hoa", "HOA", "an HOA, and anything owed to it"),
    ("code_liens", "Code violations or liens", "code violations or other liens"),
    ("moving_to", "Where they are going", "where the seller is going after the sale"),
    ("needs_time", "Time after closing", "whether they need time to stay after closing"),
)
ANSWERS = PILLARS + INTAKE
KEYS = tuple(k for k, _, _ in ANSWERS)
LABELS = {k: label for k, label, _ in ANSWERS}

# Answer -> custom field label. Ty named four (asking price, condition, occupancy, mortgage); the
# pillars that change the offer join them. Timeline exists on prod-account; the others are created once.
DEFAULT_FIELDS = {"reason": "Motivation", "timeline": "Timeline", "condition": "Condition",
                  "asking_price": "Asking Price", "occupancy": "Occupancy", "mortgage_balance": "Mortgage Balance",
                  "months_behind": "Months Behind"}
CALL_EVENTS = ("owner.call.answered", "owner.call.received")     # the call event types a stage may name
DEFAULTS = {
    "min_seconds": 90,              # a shorter answered call is a greeting or a voicemail
    "prospect_seconds": 180,        # a call this long counts on a prospect that is not a lead yet
    "call_events": ["owner.call.answered"],
    "first_days": 2,                # how far back the first run reaches
    "settle_minutes": 15,           # a call this fresh may not have its recording yet
    "max_age_days": 7,              # a call still not done after this long is closed
    "max_transcriptions": 12,       # a run
    "max_model_calls": 12,          # a run
    "max_reads": 60,                # DataSift record reads a run (about two a call)
    "max_minutes": 30,              # a longer recording is not sent
    "max_mb": 40,
    "audio_per_min": 0.003,         # transcription estimate; the bill OpenRouter returns is what is booked
    "extract_usd": 0.003,           # the notes estimate; the tokens Claude reports are what is booked
    "model": model_registry.model("call_review"),
    "max_tokens": 3000,
    "max_tries": 3,                 # a recording that will not open, a reply with no answers
    "recording_grace_hours": 24,    # a recording that is gone this soon is tried again
    "keep_days": 60,                # transcripts on the box
    "keep_calls": 20,               # call notes kept per record for the handoff note
    "fields": dict(DEFAULT_FIELDS),
    "overwrite_fields": False,      # True: a newer call replaces a value a person typed
    "handoff_statuses": ["Hot Lead", "underwrite"],
    "handoff_delay_minutes": 45,    # so the call that made the lead hot is noted first
    "handoff_wait_hours": 24,       # longest a What we know note waits for a call still being noted
    "sync": True,
    "sync_calls": 60,
}
DONE = ("noted", "skipped", "closed")
INDEX = "leadnotes"                 # ledger fact on a record: the answers of every call noted on it
FIELDS_FACT = "leadnotes:fields"    # what this stage last wrote in each custom field of a record
SUMMARY_FACT = "leadnotes:summary"  # the calls the last What we know note merged
MIN_QUOTE_WORDS = 2
NOTE_MAX = 3900                     # the add-notes route keeps 4,000 characters
MAX_CHARS = 60000                   # transcript characters sent (a 30 minute call is well under it)
UNOPENED = {"login": "the recording link needs a login the brain does not have",
            "gone": "the recording is gone", "not_audio": "the recording link did not return audio",
            "too_big": "the recording is too large to send", "bad_link": "the log's recording link is not a web address",
            "error": "the recording did not open"}
UNOPENED_SHORT = {"login": "needs a login", "gone": "gone", "not_audio": "not audio", "too_big": "too large",
                  "bad_link": "no usable link", "error": "did not open"}
STATUS_NAMES = {"hot lead": "Hot Lead", "underwrite": "acquisitions (Underwrite)",
                "under_contract": "transactions (Under Contract)", "push_deal": "dispo (Push Deal)"}
NUMBER_WORDS = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
                "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
                "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
                "grand", "half"}
UNKNOWN_WORDS = {"", "unknown", "n/a", "na", "none given", "not discussed", "not mentioned", "not stated", "not asked",
                 "not said", "not covered", "not known", "unclear", "no answer", "not provided", "not specified"}
EM, EN = chr(0x2014), chr(0x2013)

_NOTE_RX = re.compile(r"\[[^\]]*\]")
_LABEL_RX = re.compile(r"(?:^|\s)(?:agent_vm|agent|seller|voicemail)\s*:")
_NOT_WORD_RX = re.compile(r"[^a-z0-9' ]+")
_NUM_RX = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ELLIPSIS_RX = re.compile(r"\.\.\.|" + chr(0x2026))
_SELLER_RX = re.compile(r"^\s*seller\s*:", re.I | re.M)
_FENCE_RX = re.compile(r"^```[a-z]*\s*$", re.I | re.M)


# ---------------------------------------------------------------------- the prompt
SYSTEM_HEAD = """You read the transcript of one phone call between a real estate investor's team member and the
owner of a house, and you fill in the team's lead notes.

The one rule that matters: every answer must be backed by a quote copied word for word from the transcript.
Copy the quote exactly as it is written, without the speaker label and without bracketed notes such as
[pause]. Use the seller's own words where you can. When the call does not answer an item, its value is
"Unknown" and its quote is "". Never guess. Never fill an item from what is usual for a house, an area or a
seller. Never combine two answers into a new one, and never work a number out. Write every number exactly as
the seller said it.

Keep each value to a few plain words."""


def _system() -> str:
    items = "\n".join("- %s: %s" % (k, what) for k, _, what in ANSWERS)
    shape = "{" + ", ".join('"%s": {"value": "...", "quote": "..."}' % k for k in KEYS[:2]) + ", ...}"
    return (SYSTEM_HEAD + "\n\nThe items:\n" + items + "\n\nAnswer with one JSON object and nothing else. Give every "
            "item as a key, each with a value and a quote:\n" + shape)


SYSTEM = _system()


def user_text(transcript: str) -> str:
    return ("The call to read. The AGENT and SELLER labels come from a transcription pass and can be swapped: the "
            "seller is the person who owns the house.\n\nTRANSCRIPT:\n" + str(transcript or "")[:MAX_CHARS])


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with a lead notes block, for the playbook checker: a typo fails `autopilot check`
    instead of reading as a default on the first live morning."""
    out = []
    for key, low in (("min_seconds", 1), ("prospect_seconds", 1), ("max_transcriptions", 0), ("max_model_calls", 0),
                     ("max_reads", 1), ("max_tries", 1), ("max_tokens", 500), ("sync_calls", 1), ("keep_days", 1),
                     ("keep_calls", 1)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    for key in ("first_days", "settle_minutes", "max_age_days", "max_minutes", "max_mb", "audio_per_min", "extract_usd",
                "recording_grace_hours", "handoff_delay_minutes", "handoff_wait_hours"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a number, 0 or more" % (st.id, key))
    lo, hi = st.get("min_seconds"), st.get("prospect_seconds")
    lo = lo if isinstance(lo, int) and not isinstance(lo, bool) else DEFAULTS["min_seconds"]
    hi = hi if isinstance(hi, int) and not isinstance(hi, bool) else DEFAULTS["prospect_seconds"]
    if hi < lo:
        out.append("stage %s: prospect_seconds (a real conversation on a prospect) may not be shorter than min_seconds"
                   % st.id)
    ev = st.get("call_events")
    if ev is not None and (not isinstance(ev, list) or not ev or [x for x in ev if str(x) not in CALL_EVENTS]):
        out.append("stage %s: call_events lists call events from %s" % (st.id, ", ".join(CALL_EVENTS)))
    f = st.get("fields")
    if f is not None:
        if not isinstance(f, dict):
            out.append("stage %s: fields maps an answer to a custom field label" % st.id)
        else:
            bad = sorted(str(k) for k in f if str(k) not in KEYS)
            if bad:
                out.append("stage %s: fields names %s; the answers are %s" % (st.id, bad, ", ".join(KEYS)))
            if [v for v in f.values() if not str(v or "").strip()]:
                out.append("stage %s: every field label in fields must be filled in" % st.id)
    hs = st.get("handoff_statuses")
    if hs is not None and (not isinstance(hs, list) or [x for x in hs if not str(x or "").strip()]):
        out.append("stage %s: handoff_statuses lists the statuses (as the account stores them) that mean a handoff" % st.id)
    if st.get("model") is not None and not str(st.get("model") or "").strip():
        out.append("stage %s: model may not be empty" % st.id)
    for key in ("overwrite_fields", "sync"):
        if st.get(key) is not None and not isinstance(st.get(key), bool):
            out.append("stage %s: %s is true or false" % (st.id, key))
    return out


# ---------------------------------------------------------------------- what the call said
def normalize(text: str) -> str:
    """What was said, words only and in lower case: speaker labels, bracketed delivery notes and
    punctuation are not part of it. The same reading the coaching grader uses for its quote check."""
    s = str(text or "").lower().replace(chr(0x2019), "'").replace(chr(0x2018), "'")
    s = _NOTE_RX.sub(" ", s)
    s = _LABEL_RX.sub(" ", s)
    s = _NOT_WORD_RX.sub(" ", s)
    return " ".join(s.split())


def said(quote: str, hay: str) -> bool:
    """Is this quote in the call (`hay` is the normalized transcript)? An ellipsis joins pieces, and each
    piece must be there as whole words. A piece under two words proves nothing: "yes" is in every call."""
    pieces = [normalize(p) for p in _ELLIPSIS_RX.split(quote or "")]
    pieces = [p for p in pieces if p]
    if not pieces:
        return False
    padded = " %s " % hay
    return all(len(p.split()) >= MIN_QUOTE_WORDS and (" %s " % p) in padded for p in pieces)


def _stems(text: str) -> set[str]:
    out = set()
    for m in _NUM_RX.findall(text or ""):
        d = m.replace(",", "").replace(".", "").strip("0")
        out.add(d or "0")
    return out


def numbers_hold(value: str, quote: str) -> bool:
    """Every number in the value is in its quote (150 and "150 thousand" agree, so do 40,000 and
    "40 thousand"), or the quote says its number in words. A number the quote does not hold was worked
    out or made up, and an invented mortgage balance is worse than an Unknown one."""
    need = _stems(value)
    if not need:
        return True
    have = _stems(quote)
    if need <= have:
        return True
    return not have and bool(set(normalize(quote).split()) & NUMBER_WORDS)


def is_unknown(value: str) -> bool:
    v = str(value or "").strip().strip(".").strip().lower()
    return v in UNKNOWN_WORDS or v.startswith(("unknown", "not mentioned", "not discussed"))


def tidy(text, limit: int = 0) -> str:
    """One line a person reads: no long dash or en dash, no surrounding quote marks, short."""
    t = str(text or "").replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-")
    t = " ".join(t.split()).strip().strip('"').strip(chr(0x201C) + chr(0x201D)).strip()
    if limit and len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0] or t[:limit]
        t = cut.rstrip(",;: ") + "..."
    return t


def tidy_transcript(text: str) -> str:
    t = str(text or "").replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-")
    return _FENCE_RX.sub("", t).strip()


def validate(raw: dict, transcript: str) -> tuple[dict, dict]:
    """(the answers that hold, why each other answer was dropped). The hard rule: a value with no quote,
    a quote that is not in the call, or a number its quote does not hold is Unknown, never written."""
    hay = normalize(transcript)
    keep, dropped = {}, {}
    for k in KEYS:
        item = (raw or {}).get(k)
        if isinstance(item, str):
            item = {"value": item, "quote": ""}
        if not isinstance(item, dict):
            continue
        value, quote = tidy(item.get("value"), 120), tidy(item.get("quote"), 300)
        if is_unknown(value):
            continue
        if not quote:
            dropped[k] = "no quote"
        elif not said(quote, hay):
            dropped[k] = "the quote is not in the call"
        elif not numbers_hold(value, quote):
            dropped[k] = "a number its quote does not hold"
        else:
            keep[k] = {"value": value, "quote": quote}
    return keep, dropped


def parse_reply(text: str) -> dict | None:
    """The answers object out of the model's reply, or None. A reply with no answers in it is a call to
    try again, never an empty note."""
    from call_coaching import grade
    for b in grade.json_blocks(text or ""):
        if any(k in b for k in KEYS):
            return b
    return None


# ---------------------------------------------------------------------- the notes
def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _minutes(seconds) -> str:
    return "%d min" % max(1, round(_int(seconds) / 60.0))


def _when(dt: datetime, tz: str) -> str:
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %s %d, %d at %s" % (d.strftime("%a"), d.strftime("%b"), d.day, d.year, d.strftime("%I:%M %p").lstrip("0"))


def _day(dt: datetime | None, tz: str) -> str:
    if dt is None:
        return ""
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %d" % (d.strftime("%b"), d.day)


def _parse_when(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v or ""))
    except ValueError:
        return None


def _plain(label: str) -> str:
    return label if label.isupper() else label[:1].lower() + label[1:]


def _status_name(status: str) -> str:
    return STATUS_NAMES.get(str(status or "").strip().lower(), str(status or "").strip() or "the next stage")


def _quoted(a: dict, day: str = "", limit: int = 140) -> str:
    q = tidy(a.get("quote"), limit)
    tail = ('"%s"' % q) if q else ""
    if day:
        tail = ("%s, %s" % (tail, day)) if tail else day
    return ("%s (%s)" % (a.get("value"), tail)) if tail else str(a.get("value") or "")


def _answer_lines(answers: dict, limit: int, days: dict | None = None) -> list[str]:
    """The four pillars always, Unknown when the call did not say; then each intake answer known. An
    answer whose quote was already printed gives its value alone, so one sentence is not shown twice."""
    out, shown = [], set()
    days = days or {}
    for k, label, _ in PILLARS:
        a = answers.get(k)
        out.append("%s: %s" % (label, _quoted(a, days.get(k, ""), limit) if a else UNKNOWN))
        if a:
            shown.add(normalize(a.get("quote")))
    for k, label, _ in INTAKE:
        a = answers.get(k)
        if not a:
            continue
        q = normalize(a.get("quote"))
        out.append("%s: %s" % (label, str(a.get("value") or "") if q in shown else _quoted(a, days.get(k, ""), limit)))
        shown.add(q)
    return out


FOOTER = "From the call recording. Each answer quotes the call; what the call did not say is Unknown."


def note_text(answers: dict, when: datetime, caller: str, seconds, inbound: bool, tz: str, limit: int = 140) -> str:
    """The one format every call note is written in (the lesson: one trained format every time)."""
    who = (caller or "").strip() or "a team member the log does not name"
    how = ("the seller called in and %s answered" % who) if inbound else ("%s called" % who)
    lines = ["Lead notes from the call on %s (%s, %s)." % (_when(when, tz), how, _minutes(seconds))]
    lines += _answer_lines(answers, limit)
    gaps = [_plain(label) for k, label, _ in INTAKE if k not in answers]
    if gaps:
        lines.append("Not covered on this call: %s." % ", ".join(gaps))
    lines.append(FOOTER)
    text = "\n".join(lines)
    if len(text) > NOTE_MAX and limit > 40:
        return note_text(answers, when, caller, seconds, inbound, tz, limit=40)
    return text[:NOTE_MAX]


def summary_text(calls: list, status: str, tz: str, limit: int = 140) -> str:
    """The What we know note at a handoff: every call note on file merged, the newest answer to each
    item winning, each with the day it was said (C8.21: confirm, never ask again)."""
    newest = sorted(calls, key=lambda c: str(c.get("at") or ""), reverse=True)
    pick, days = {}, {}
    for c in newest:
        when = _parse_when(c.get("at"))
        for k, a in (c.get("answers") or {}).items():
            if k in LABELS and k not in pick and isinstance(a, dict) and a.get("value"):
                pick[k] = a
                days[k] = _day(when, tz)
    whens = [w for w in (_parse_when(c.get("at")) for c in calls) if w]
    span = ""
    if whens:
        a, b = _day(min(whens), tz), _day(max(whens), tz)
        span = (" on %s" % a) if a == b else (" between %s and %s" % (a, b))
    lines = ["What we know before the handoff to %s, from %s%s (the newest answer to each item)." % (
        _status_name(status), _plural(len(calls), "call"), span)]
    lines += _answer_lines(pick, limit, days)
    gaps = [_plain(label) for k, label, _ in INTAKE if k not in pick]
    if gaps:
        lines.append("Not covered on any call: %s." % ", ".join(gaps))
    lines.append("Confirm these with the seller; do not ask them again.")
    text = "\n".join(lines)
    if len(text) > NOTE_MAX and limit > 40:
        return summary_text(calls, status, tz, limit=40)
    return text[:NOTE_MAX]


# ---------------------------------------------------------------------- the paid pieces, replaceable
def _engine():
    from call_coaching import grade, transcribe
    return grade, transcribe


def _fetch(url: str, max_bytes: int) -> dict:
    if FETCH is not None:
        return FETCH(url, max_bytes)
    return _engine()[1].fetch_recording(url, max_bytes=max_bytes)


def _transcribe(audio: bytes, fmt: str, caller: str) -> tuple[str, dict]:
    if TRANSCRIBE is not None:
        return TRANSCRIBE(audio, fmt, caller)
    return _engine()[1].transcribe_bytes(audio, fmt, caller or None)


def _billed(usage) -> float:
    return _engine()[1].billed(usage if isinstance(usage, dict) else {})


def _usd(usage, model: str) -> float:
    return _engine()[0].usage_usd(usage, model)


def _ask(system: str, user: str, model: str, max_tokens: int) -> tuple[str, dict, str]:
    """One read of one call by Claude, the way voice._ask_claude does it: thinking off (Haiku 5.5 takes
    that at effort high or below) and effort low, since copying answers out of a transcript needs no
    reasoning. A model that refuses "disabled" (Opus 5.5, Sonnet 5.5) is asked again without it."""
    if ASK is not None:
        return ASK(system, user, model, max_tokens)
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    base = dict(model=model, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}])
    try:
        msg = client.messages.create(max_tokens=max_tokens, thinking={"type": "disabled"}, **base)
    except anthropic.BadRequestError:
        msg = client.messages.create(max_tokens=max_tokens + 4000, **base)
    u = getattr(msg, "usage", None)
    usage = {k: int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                      "cache_creation_input_tokens")}
    text = "".join(getattr(b, "text", "") or "" for b in (msg.content or []) if getattr(b, "type", "") == "text")
    return text, usage, str(getattr(msg, "stop_reason", "") or "")


def keys_missing() -> str:
    """The keys a live run needs and the box does not have, or "". Checked before the first paid step:
    paying to transcribe a call whose notes then cannot be written is money for nothing."""
    out = []
    if TRANSCRIBE is None:
        try:
            _engine()[1]._env("OPENROUTER_API_KEY")
        except KeyError:
            out.append("OPENROUTER_API_KEY")
    if ASK is None and not str(config.ANTHROPIC_API_KEY or "").strip():
        out.append("ANTHROPIC_API_KEY")
    return " and ".join(out)


def _coach_transcript(cid: str) -> str:
    """A transcript the coaching workflow already paid for (it keys calls by the same log id)."""
    try:
        from ..workflows import call_coaching
        row = call_coaching.stored(cid) or {}
    except Exception:  # noqa: BLE001  (no coaching table yet: nothing to reuse)
        return ""
    return str(row.get("transcript") or "")


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


# ---------------------------------------------------------------------- the stage's own memory
def transcripts_dir() -> Path:
    return Path(config.DATA_DIR) / "autopilot" / "leadnotes" / "transcripts"


def transcript_file(cid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(cid or ""))[:80].strip(".") or "call"
    if safe != str(cid):
        safe += "-" + hashlib.sha256(str(cid).encode("utf-8")).hexdigest()[:8]
    return transcripts_dir() / ("%s.txt" % safe)


def prune(keep_days: float) -> int:
    """Delete this stage's transcripts older than keep_days (privacy: a seller's call is not kept for
    ever). Only the .txt files in its own folder."""
    d = transcripts_dir()
    if not d.is_dir():
        return 0
    cutoff = time.time() - float(keep_days) * 86400
    n = 0
    for f in d.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n


def _mark_key(live: bool) -> str:
    return "leadnote" if live else "leadnote_plan"


def mark(subject: str, live: bool) -> dict:
    """What this stage decided about one call or one handoff. Plan and live keep separate marks, or a
    live run would skip every call a plan run only counted."""
    v = ledger.fact(subject, _mark_key(live))
    return v if isinstance(v, dict) else {}


def _set_mark(subject: str, live: bool, status: str, now: datetime, **detail) -> None:
    ledger.set_fact(subject, _mark_key(live), dict(detail, status=status, at=now.isoformat(timespec="seconds")))


def _tries(m: dict) -> int:
    return _int(m.get("tries")) if m.get("status") == "open" else 0


def calls_on_file(record_uuid: str) -> list[dict]:
    """The call notes kept for a record, oldest first: {call, at, caller, answers}."""
    v = ledger.fact(record_uuid, INDEX) or {}
    return [c for c in (v.get("calls") or []) if isinstance(c, dict)] if isinstance(v, dict) else []


# ---------------------------------------------------------------------- one run
class _Run:
    def __init__(self, ctx):
        self.ctx = ctx
        self.st = settings(ctx.stage)
        self.live = bool(ctx.live)
        self.tz = ctx.policy.tz
        self.reads = self.transcribed = self.asked = 0
        self.houses: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.field_ids: dict[str, str] | None = None
        self.missing: list[str] = []
        self.refused: set[str] = set()
        self.need_fields = False
        self.noted: list[tuple] = []          # live: (street, answers, pillars)
        self.would: list[tuple] = []          # plan: (street, minutes)
        self.summaries: list[str] = []
        self.would_summaries: list[str] = []
        self.waiting: Counter = Counter()     # calls left for the next run, by what held them
        self.held_summaries = 0               # What we know notes waiting for a call still being noted
        self.unopened: Counter = Counter()
        self.hosts: dict[str, set] = {}
        self.open_owners: set[str] = set()

    # ------------------------------------------------------------------ the pieces a call needs
    def _kv(self, what: str) -> str:
        return "autopilot:leadnotes:%s:%s:%s" % (self.ctx.stage.id, what, "live" if self.live else "plan")

    def mark_time(self, what: str) -> datetime | None:
        return _parse_when(store.kv_get(self._kv(what)))

    def set_mark_time(self, what: str, at: datetime) -> None:
        store.kv_set(self._kv(what), at.isoformat(timespec="seconds"))

    def _read(self) -> bool:
        if self.reads >= int(self.st["max_reads"]):
            self.waiting["the run's read cap"] += 1
            return False
        self.reads += 1
        return True

    def house(self, owner: str) -> dict | None:
        """The one house a call on this owner is about, or a reason it cannot be told. None: not this
        run (the read cap, or DataSift did not answer)."""
        if owner in self.houses:
            return self.houses[owner]
        if not owner or not self._read():
            return None
        try:
            rows = list(self.ctx.crm.owner_records(owner, cap=10) or [])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not look up the house for a called owner (%s)" % str(exc)[:80])
            return None
        if not rows:
            v = {"skip": "no house on file for the owner"}
        else:
            pick = rows if len(rows) == 1 else [r for r in rows if lanes.status_of(r) in lanes.LEAD_STATUSES]
            if len(pick) != 1:
                v = {"skip": "the owner holds several houses and the call does not say which one"}
            elif _is_entity(pick[0].get("owner") or {}):
                v = {"skip": "a company or trust owns it (people only)"}
            else:
                v = {"row": pick[0]}
        self.houses[owner] = v
        return v

    def record(self, u: str) -> dict | None:
        if u in self.records:
            return self.records[u]
        if not u or not self._read():
            return None
        try:
            rec = self.ctx.crm.record(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read a called owner's record (%s)" % str(exc)[:80])
            return None
        rec.setdefault("uuid", u)
        self.records[u] = rec
        return rec

    def long_call(self, owner: str) -> bool:
        need = int(self.st["prospect_seconds"])
        return any(_int((e.get("data") or {}).get("duration")) >= need
                   for e in activity.events(tuple(self.st["call_events"]), resource=owner))

    def has_call(self, owner: str) -> bool:
        need = int(self.st["min_seconds"])
        return any(_int((e.get("data") or {}).get("duration")) >= need and (e.get("data") or {}).get("recording")
                   for e in activity.events(tuple(self.st["call_events"]), resource=owner))

    def known_transcript(self, cid: str) -> str:
        f = transcript_file(cid)
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8")
            except OSError:
                return ""
        return _coach_transcript(cid)

    # ------------------------------------------------------------------ marks
    def done(self, subject: str, status: str, **detail) -> str:
        _set_mark(subject, self.live, status, self.ctx.now, **detail)
        return "done"

    def skip(self, subject: str, why: str, tally: bool = True) -> str:
        if tally and why:
            self.ctx.out.tally["skipped: %s" % why] += 1
        return self.done(subject, "skipped", why=why or "held back by the gate")

    def close(self, subject: str, why: str) -> str:
        self.ctx.out.tally["closed: %s" % why] += 1
        _set_mark(subject, self.live, "closed", self.ctx.now, why=why)
        return "closed"

    def bump(self, subject: str, m: dict, why: str, **extra) -> str:
        tries = _tries(m) + 1
        if tries >= int(self.st["max_tries"]):
            return self.close(subject, why)
        _set_mark(subject, self.live, "open", self.ctx.now, tries=tries, why=why, **extra)
        self.waiting["a try again (%s)" % why] += 1
        return "open"

    # ------------------------------------------------------------------ the run
    def run(self) -> None:
        self.sync()
        gap = keys_missing() if self.live else ""
        if gap:
            self.ctx.out.problem("Lead notes need %s on the brain, so no call was transcribed" % gap)
        else:
            self.calls()
        if self.st["handoff_statuses"]:
            self.handoffs()
        if self.live:
            prune(float(self.st["keep_days"]))
        self.report()

    def sync(self) -> None:
        if not self.st["sync"]:
            return
        for g in ["calls"] + (["state"] if self.st["handoff_statuses"] else []):
            try:
                activity.sync(self.ctx.crm, g, now=self.ctx.now, max_calls=int(self.st["sync_calls"]))
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (a stale copy still holds the calls it has)
                self.ctx.out.problem("could not refresh the %s log before the lead notes (%s)" % (g, str(exc)[:80]))

    def calls(self) -> None:
        """Every answered call since the last run, oldest first. The mark of how far it got moves only
        past calls that are decided, so a call held by a cap or waiting on its recording is read again."""
        st, ctx = self.st, self.ctx
        until = ctx.now - timedelta(minutes=float(st["settle_minutes"]))
        start = self.mark_time("calls") or (ctx.now - timedelta(days=float(st["first_days"])))
        floor = ctx.now - timedelta(days=float(st["max_age_days"]))
        open_at, seen, found = [], set(), 0
        for e in activity.events(tuple(st["call_events"]), since=start - activity.OVERLAP, until=until):
            d = e.get("data") or {}
            secs = _int(d.get("duration"))
            if not d.get("recording") or secs < int(st["min_seconds"]):
                continue
            cid = str(d.get("call") or e.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            subject = "call:%s" % cid
            m = mark(subject, self.live)
            if m.get("status") in DONE:
                continue
            found += 1
            if e["when"] < floor:
                self.close(subject, "waited more than %s" % _plural(int(float(st["max_age_days"])), "day"))
                continue
            if self.one(e, cid, subject, m, secs) == "open":
                open_at.append(e["when"])
                self.open_owners.add(str(e.get("resource") or ""))
        ctx.out.found = found
        self.set_mark_time("calls", min(open_at) if open_at else until)

    def one(self, e: dict, cid: str, subject: str, m: dict, secs: int) -> str:
        """One call: "done" (noted or skipped for good), "closed" (given up, with the reason) or "open"
        (try again next run)."""
        st, ctx = self.st, self.ctx
        owner = str(e.get("resource") or "")
        house = self.house(owner)
        if house is None:
            return "open"
        if house.get("skip"):
            return self.skip(subject, house["skip"])
        row = house["row"]
        if lanes.status_of(row) not in lanes.LEAD_STATUSES and not self.long_call(owner):
            return self.skip(subject, "not a lead, and no call of %s or more" % _minutes(int(st["prospect_seconds"])))
        rec = self.record(str(row.get("uuid") or ""))
        if rec is None:
            return "open"
        if _is_entity(rec.get("owner") or {}):
            return self.skip(subject, "a company or trust owns it (people only)")
        if not ctx.passes(rec, "research"):
            return self.skip(subject, "", tally=False)          # the gate's reason is already counted
        if secs > float(st["max_minutes"]) * 60:
            return self.skip(subject, "longer than %d minutes" % int(float(st["max_minutes"])))
        u = rec["uuid"]
        minutes = secs / 60.0
        answers = m.get("answers") if self.live and isinstance(m.get("answers"), dict) else None
        text = "" if answers is not None else self.known_transcript(cid)
        est_t = 0.0 if (text or answers is not None) else round(max(1.0, minutes) * float(st["audio_per_min"]), 4)
        est_x = 0.0 if answers is not None else float(st["extract_usd"])
        if est_t and self.transcribed >= int(st["max_transcriptions"]):
            self.waiting["the run's transcription cap"] += 1
            return "open"
        if est_x and self.asked >= int(st["max_model_calls"]):
            self.waiting["the run's model call cap"] += 1
            return "open"
        # The record is not named: its lifetime cap is for traces and scoring, and a lead whose numbers
        # cost a dollar still gets its notes. The day caps (the stage's, the autopilot's, the brain's) apply.
        ok, why = ctx.budget(est_t + est_x)
        if not ok:
            self.waiting[why] += 1
            return "open"
        ctx.reserve(est_t + est_x)
        street = addr(rec)
        if not self.live:
            self.need_fields = True           # a note it would write needs the fields: name the missing ones
            self.would.append((street, max(1, round(minutes))))
            self.transcribed += 1 if est_t else 0
            self.asked += 1 if est_x else 0
            return self.done(subject, "noted", record=u)
        if answers is None:
            if not text:
                text, state = self.listen(e, cid, subject, m, u, est_t)
                if not text:
                    ctx.settle(est_x, 0, "lead notes")
                    return state
            if not _SELLER_RX.search(text):
                ctx.settle(est_x, 0, "lead notes")
                return self.close(subject, "no seller on the recording (a voicemail or a greeting)")
            raw, why = self.extract(text, est_x, u, cid)
            if raw is None:
                return self.bump(subject, m, why)
            answers, dropped = validate(raw, text)
            for w in dropped.values():
                ctx.out.tally["answers left Unknown: %s" % w] += 1
            if not answers:
                # A note of nothing but Unknowns is clutter the team learns to skip, so none is written.
                return self.skip(subject, "the call gave none of the answers")
        d = e.get("data") or {}
        caller = str(d.get("caller") or "")
        note = note_text(answers, e["when"], caller, secs, d.get("direction") == "inbound", self.tz)
        try:
            ctx.write("lead note", ctx.crm.message, u, note)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the answers are kept: the next run writes the note without paying again)
            ctx.out.problem("could not write a lead note on a record (%s)" % str(exc)[:80])
            return self.bump(subject, m, "the note could not be written", answers=answers)
        self.done(subject, "noted", record=u, answers=len(answers))
        self.need_fields = True
        self.remember(u, cid, e, caller, answers)
        ledger.event(ctx.run_id, ctx.stage.id, u, "leadnote", {"call": cid, "step": "note", "answers": len(answers)},
                     now=ctx.now, tz=self.tz)
        self.write_fields(rec, answers)
        self.noted.append((street, len(answers), sum(1 for k, _, _ in PILLARS if k in answers)))
        return "done"

    def listen(self, e: dict, cid: str, subject: str, m: dict, u: str, est_t: float) -> tuple[str, str]:
        """The recording, opened from the log's own link and transcribed. ("", state) when there is no
        transcript this run: a recording the dialer has not finished is tried again, a login is never
        guessed (the call is closed and named as a problem)."""
        st, ctx = self.st, self.ctx
        d = e.get("data") or {}
        got = _fetch(str(d.get("recording") or ""), int(float(st["max_mb"]) * 1_000_000)) or {}
        if not got.get("ok"):
            ctx.settle(est_t, 0, "transcription")
            code = str(got.get("code") or "error")
            code = code if code in UNOPENED else "error"
            if code == "gone" and ctx.now - e["when"] < timedelta(hours=float(st["recording_grace_hours"])):
                self.waiting["a recording the dialer has not finished"] += 1
                return "", "open"
            if code == "error" and _tries(m) + 1 < int(st["max_tries"]):
                return "", self.bump(subject, m, "the recording did not open")
            self.unopened[code] += 1
            if got.get("host"):
                self.hosts.setdefault(code, set()).add(str(got["host"]))
            return "", self.close(subject, UNOPENED[code])
        try:
            text, usage = _transcribe(got.get("audio") or b"", str(got.get("fmt") or "mp3"), str(d.get("caller") or ""))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (OpenRouter down: this call waits, the run goes on)
            ctx.settle(est_t, 0, "transcription")
            return "", self.bump(subject, m, "the transcription failed (%s)" % str(exc)[:60])
        cost = _billed(usage) or est_t
        ctx.settle(est_t, cost, "transcribed a call")
        ledger.event(ctx.run_id, ctx.stage.id, u, "leadnote", {"call": cid, "step": "transcribe"}, usd=cost,
                     now=ctx.now, tz=self.tz)
        self.transcribed += 1
        text = tidy_transcript(text)
        if not text:
            return "", self.close(subject, "the transcription came back empty")
        try:
            f = transcript_file(cid)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("lead notes: could not keep a transcript (%s); a retry would pay for it again", exc)
        return text, "ok"

    def extract(self, text: str, est_x: float, u: str, cid: str) -> tuple[dict | None, str]:
        st, ctx = self.st, self.ctx
        model = str(st["model"])
        try:
            reply, usage, stop = _ask(SYSTEM, user_text(text), model, int(st["max_tokens"]))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the model down: the transcript is kept, the call waits)
            ctx.settle(est_x, 0, "lead notes")
            return None, "the notes model did not answer (%s)" % str(exc)[:60]
        cost = _usd(usage, model) or est_x
        ctx.settle(est_x, cost, "lead notes from a call")
        ledger.event(ctx.run_id, ctx.stage.id, u, "leadnote", {"call": cid, "step": "notes"}, usd=cost,
                     now=ctx.now, tz=self.tz)
        self.asked += 1
        if str(stop or "") in ("refusal", "max_tokens"):
            return None, "the notes came back cut off or refused"
        raw = parse_reply(reply)
        if raw is None:
            return None, "the notes came back without the answers"
        return raw, ""

    def remember(self, u: str, cid: str, e: dict, caller: str, answers: dict) -> None:
        calls = [c for c in calls_on_file(u) if c.get("call") != cid]
        calls.append({"call": cid, "at": e["when"].isoformat(timespec="seconds"), "caller": caller, "answers": answers})
        calls.sort(key=lambda c: str(c.get("at") or ""))
        ledger.set_fact(u, INDEX, {"calls": calls[-int(self.st["keep_calls"]):]})

    def ids(self) -> dict:
        """label -> custom field uuid, "" for a label the account does not have. Read once a run."""
        if self.field_ids is None:
            out = {}
            for lab in dict.fromkeys(str(v) for v in (self.st["fields"] or {}).values()):
                try:
                    out[lab] = self.ctx.crm.custom_field_uuid(lab) or ""
                except budget.BudgetExhausted:
                    raise
                except Exception as exc:  # noqa: BLE001
                    self.ctx.out.problem("could not read the account's custom fields (%s)" % str(exc)[:80])
                    out[lab] = ""
            self.field_ids = out
            self.missing = [lab for lab, fu in out.items() if not fu]
        return self.field_ids

    def write_fields(self, rec: dict, answers: dict) -> None:
        """The answers whose custom fields exist. An empty field, or one holding Unknown, takes the call's
        answer; a field this stage wrote before takes the newer answer; a field a person (or another
        stage) filled in is left as it is, the answer staying in the note (overwrite_fields: true replaces it)."""
        st, ctx = self.st, self.ctx
        ids = self.ids()
        todo = {k: str(lab) for k, lab in (st["fields"] or {}).items()
                if k in answers and ids.get(str(lab)) and str(lab) not in self.refused}
        if not todo:
            return
        u = rec["uuid"]
        if not self._read():
            ctx.out.tally["fields not written this run (the read cap)"] += 1
            return
        try:
            current = ctx.crm.custom_fields(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a record's custom fields, so no call answer went into a field (%s)"
                            % str(exc)[:80])
            return
        cur = {str(k).strip().lower(): v for k, v in current.items()}
        mine = ledger.fact(u, FIELDS_FACT) or {}
        mine = mine if isinstance(mine, dict) else {}
        wrote = dict(mine)
        for k, lab in todo.items():
            new = str(answers[k].get("value") or "").strip()
            before = cur.get(lab.lower())
            before = "" if before is None else str(before)
            if not new or before.strip() == new:
                continue
            # "Unknown" is a placeholder, not an answer (the encumbrance stage writes it into an empty intake
            # field when a record becomes a lead): the call's answer replaces it.
            if before.strip() and not is_unknown(before) and before != mine.get(lab) and not st["overwrite_fields"]:
                ctx.out.tally["kept a value a person typed in %s" % lab] += 1
                continue
            fu = ids[lab]
            ctx.change(u, "custom_field", {"field_uuid": fu, "value": before}, {"field_uuid": fu, "value": new})
            try:
                ctx.write("intake field", ctx.crm.set_custom_field, u, fu, new)
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001  (a pick list wants an option id, not words)
                self.refused.add(lab)
                ctx.out.problem("DataSift refused a call answer for the %s field, so it is in the note only; make it a "
                                "text field (%s)" % (lab, str(exc)[:60]))
                continue
            wrote[lab] = new
            ctx.out.tally["fields written"] += 1
        if wrote != mine:
            ledger.set_fact(u, FIELDS_FACT, wrote)

    # ------------------------------------------------------------------ handoffs
    def handoffs(self) -> None:
        """Every move into a handoff status since the last run, once it is `handoff_delay_minutes` old,
        so the call that made the lead hot has its note first."""
        st, ctx = self.st, self.ctx
        want = {str(s).strip().lower() for s in st["handoff_statuses"] if str(s).strip()}
        until = ctx.now - timedelta(minutes=float(st["handoff_delay_minutes"]))
        start = self.mark_time("handoffs") or (ctx.now - timedelta(days=float(st["first_days"])))
        open_at = []
        for e in activity.events("property.status.updated", since=start - activity.OVERLAP, until=until):
            to = str((e.get("data") or {}).get("to") or "").strip()
            if to.lower() not in want:
                continue
            subject = "handoff:%s" % e.get("id")
            if mark(subject, self.live).get("status") in DONE:
                continue
            if self.handoff(e, subject, to) == "open":
                open_at.append(e["when"])
        self.set_mark_time("handoffs", min(open_at) if open_at else until)

    def handoff(self, e: dict, subject: str, to: str) -> str:
        st, ctx = self.st, self.ctx
        u = str(e.get("resource") or "")
        rec = self.record(u)
        if rec is None:
            return "open"
        owner = rec.get("owner") or {}
        if _is_entity(owner):
            return self.skip(subject, "a company or trust owns it (people only)")
        if not ctx.passes(rec, "research"):
            return self.skip(subject, "", tally=False)
        if str(owner.get("uuid") or "") in self.open_owners and \
                ctx.now - e["when"] < timedelta(hours=float(st["handoff_wait_hours"])):
            self.held_summaries += 1
            return "open"
        calls = [c for c in calls_on_file(u) if c.get("answers")]
        street = addr(rec)
        if not self.live:
            if not calls and not self.has_call(str(owner.get("uuid") or "")):
                return self.skip(subject, "moved to %s with no call on file to take notes from" % _status_name(to))
            self.would_summaries.append(street)
            return self.done(subject, "noted", record=u)
        if not calls:
            return self.skip(subject, "moved to %s with no call notes on file yet" % _status_name(to))
        sig = sorted(str(c.get("call") or "") for c in calls)
        last = ledger.fact(u, SUMMARY_FACT) or {}
        if isinstance(last, dict) and last.get("calls") == sig:
            return self.skip(subject, "no new call notes since the last What we know note")
        text = summary_text(calls, to, self.tz)
        try:
            ctx.write("what we know note", ctx.crm.message, u, text)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not write a What we know note on a record (%s)" % str(exc)[:80])
            return "open"
        self.done(subject, "noted", record=u)
        ledger.set_fact(u, SUMMARY_FACT, {"calls": sig, "status": to, "at": ctx.now.isoformat(timespec="seconds")})
        ledger.event(ctx.run_id, ctx.stage.id, u, "leadnote_summary", {"status": to, "calls": len(calls)},
                     now=ctx.now, tz=self.tz)
        self.summaries.append(street)
        return "done"

    # ------------------------------------------------------------------ what it says
    def report(self) -> None:
        ctx, out = self.ctx, self.ctx.out
        rows = self.noted if self.live else self.would
        n, houses = len(rows), len({r[0] for r in rows})
        sums = self.summaries if self.live else self.would_summaries
        wait = sum(self.waiting.values())
        out.acted = n + len(sums)
        parts = []
        if n and self.live:
            parts.append("Wrote lead notes from %s on %s" % (_plural(n, "call"), _plural(houses, "house")))
        elif n:
            parts.append("Would transcribe %s on %s (about $%.2f) and write a lead note on each" % (
                _plural(n, "call"), _plural(houses, "house"), max(out.would_usd, 0.01)))
        if sums:
            parts.append(("%s before a handoff" % _plural(len(sums), "What we know note")) if self.live else
                         ("%s would get a What we know note" % _plural(len(sums), "handoff")))
        if wait:
            parts.append("%s for the next run" % ("1 call waits" if wait == 1 else "%d calls wait" % wait))
        if self.held_summaries:
            parts.append("%s for a call still being noted" % (
                "1 What we know note waits" if self.held_summaries == 1 else "%d What we know notes wait" % self.held_summaries))
        out.head = ("; ".join(parts) + ".") if parts else ""
        if self.live:
            for street, k, p in self.noted[:6]:
                out.line("%s: %s, %d of the 4 pillars" % (street, _plural(k, "answer"), p))
        else:
            for street, mins in self.would[:6]:
                out.line("%s: a %d minute call" % (street, mins))
        for street in sums[:3]:
            out.line("%s: What we know note%s" % (street, "" if self.live else ", would write"))
        for why, k in self.waiting.most_common(2):
            out.line("Waiting: %s, held by %s." % (_plural(k, "call"), why))
        if not self.live and (n or sums):
            gap = keys_missing()
            if gap:
                out.line("Live mode needs %s on the brain." % gap)
        if self.unopened:
            bits = []
            for code, k in self.unopened.most_common():
                hosts = sorted(self.hosts.get(code) or ())
                bits.append("%s: %d%s" % (UNOPENED_SHORT[code], k, (" (%s)" % ", ".join(hosts[:2])) if hosts else ""))
            out.problem("Some call recordings could not be opened, so those calls have no lead notes: %s" % "; ".join(bits))
        if self.need_fields:
            self.ids()
            if self.missing:
                out.problem("Custom fields for the call answers do not exist yet, so those answers stay in the note "
                            "only: %s. Create them as text fields to fill them" % ", ".join(self.missing))


def run_leadnotes(ctx) -> None:
    _Run(ctx).run()
