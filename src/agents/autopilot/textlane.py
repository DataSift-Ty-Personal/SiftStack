"""The text lane (build plan 1.4 and 4.5; decisions D3, D4, D20, D21, D42).

Texting is the cheapest touch (1 to 2 cents against 3 to 6 for a call) and finds about half the
correct numbers, so a new first-to-market record is texted before it is called: text 1 the day it
lands, texts 2 and 3 on the next two days, and, once the call round is over, a follow-up series
that ends with a breakup text. Until this module the lane had no text stage at all: records were
loaded into Smarter Contact by hand, a campaign at a time.

THE STAGE NEVER SENDS. In plan mode it lists the load. In live mode it stages a batch (ap_batches,
kind "text") and stops. A person releases the batch (`cli.py autopilot release <id> --commit`);
the release runs the gate, the registry rule and the limits again on every owner, writes a load
file for the Smarter Contact loader, raises the record's text counter and counts the text. Nothing
in this file talks to a texting system.

WHAT IT READS, NEVER COPIES. When a channel opens is asked of the lanes table
(lanes.channel_open), day by day: the opening texts, the day the follow-ups start and the day they
close all come from the playbook's timeline. A change to the table changes the stage.

THE SERIES IS A SEQUENCE. A record gets the next text it has not had, never a later one first. A
record that is first eligible on day 1 (it landed after the morning run, or the death screen held
it overnight) gets text 1 on day 1 and text 2 on day 2, and the series closes when the table says
so, wherever the record is in it.

THE FOLLOW-UP SERIES (4.5) spreads the table's count over its first and last day (four texts on
days 8, 11, 15 and 18 by default), measured from the day the series opened for the record, with at
least the table's own spacing between two of them. When a late start, a cap or the platform's rest
leaves room for only one more text before the series closes, that text is the breakup: the series
always ends with it. An owner the opening texts never reached starts the series with the plain
identity check, because a first text that pitches gets filtered.

WHO IS NEVER LOADED. The gate decides (a lead, a dead or possibly dead owner, a litigator, Do Not
Market, an opt-out, a sold or listed house, mail only): it is run here and again at release and is
not re-implemented. On top of it: a record the death screen has not seen yet, an owner who already
answered (a number marked CORRECT: a person has that thread now), a number on the do-not-call
registry, and anything limits.room() refuses (one text per owner a day, five a week across both
texting systems, the platform's rest, the sending hours on both clocks).

TWO LANES, ONE STAGE (2026-11-13). The Priority track texts first-to-market owners and the Top
1,000 from the same stage (`lanes: [ftm, top1000]`): each lane is read on its own, first to market
first, under the same daily caps, so a Top 1,000 owner never takes a first-to-market owner's place.
Each record keeps its own lane's timeline, entry day and group prefix (`labels`), and the
sale-date copy goes only to the lanes in `urgent_lanes` (first to market: a Top 1,000 house has no
sale date).

ONE THREAD. The first text puts each number in a group; that group is the number's thread and
every later text to it carries the same thread key (decision D42). A sending campaign must never
gain a contact, so every send gets its own group: the day's new cohort, "<cohort> T2" the next
day, and a "Late" group when a text goes a day behind its cohort.

THE COPY. Texts 1 to 3 use the Smarter Contact templates the account already has (the Priority 1
touches: they acknowledge the noise and never the situation). The follow-up series, the
time-sensitive touches for a sale two weeks out (decision D21) and the afternoon text of plan 1.3
are written below, with one audit over every variant: one easy question, one segment at the
worst-case name and street, street only, never the list, never a number. A stage refuses to load
anything while the audit finds a problem.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import budget, gate, lanes, ledger, limits, scrub
from .stages import GONE_STATUSES, Ctx, Summary, lane_entry, required_tags, stage_lanes, stage_rows, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KIND = "text"                       # the ap_batches kind every text batch carries
SERIES = ("opening", "followup")
CLOSED_FACT = "text_closed:%s:%s"   # series, lane: this record's series is over, do not read it again
ANCHOR_FACT = "followup_anchor:%s"  # lane: the lane day the follow-up series opened for the record
UNKNOWN_FACT = "text_entry_unknown:%s"   # lane: the record's log did not show when it entered the lane
UNKNOWN_RETRY = timedelta(hours=20)      # ask the log again on a later day
UNKNOWN_TRIES = 3                        # after that many empty answers the record is taken as old
SCREEN_PLAN_HIT = "death_screen_plan_hit"
TIER_RANK = {"Dial First": 0, "Dial Second": 1}

# ---------------------------------------------------------------------- the copy
# Placeholders: {first} the owner's first name, {addr} the street line, {sender} the first name of
# the person who answers the callback. Nothing else: no city, no zip, no number.
#
# The follow-up series. 1 the plain ask, 2 what selling to us looks like (only what is true of
# every deal: as it sits, no showings, no agent fees), 3 the easy out, 4 the breakup. No variant
# says "again" or claims an earlier call or text: the series must read true for a number whose
# first message it is.
FOLLOWUP = {
    1: ([
        "Hi {first}, it's {sender}, a local buyer. Is {addr} a place you'd ever think about selling?",
        "Hey {first}, {sender} here. I was looking at {addr} the other day. Any chance you'd talk about it?",
        "Hi {first}, quick one from {sender}. Would you ever part with {addr}, even down the road?",
        "Hey {first}, it's {sender}. Is selling {addr} something you'd be willing to talk through?",
    ], [
        "Hi, it's {sender}, a local buyer. Would the owner of {addr} ever think about selling?",
        "Hey there, {sender} here. If {addr} is yours, could we talk about it sometime?",
    ]),
    2: ([
        "Hi {first}, {sender} here. I buy houses as they sit, so {addr} would need no fixing. Want to hear how it works?",
        "Hey {first}, with me there are no showings and no agent fees on {addr}. Is a quick call worth it? {sender}",
        "Hi {first}, I'd take {addr} as it is and work around your timing. Is that worth a short talk? {sender}",
        "Hey {first}, {sender} here. No repairs, no cleanup and no agent on {addr} with me. Can I explain by phone?",
    ], [
        "Hi there, {sender} here, a local buyer. {addr} could sell as it sits, nothing to fix. Could we talk by phone?",
        "Hey, it's {sender}. I can buy {addr} with no showings and no agent fees. Is the owner open to a call?",
    ]),
    3: ([
        "Hi {first}, if I have the wrong person for {addr}, tell me and I'll fix my notes. Is it yours? {sender}",
        "Hey {first}, {sender} here. I don't want to pester you about {addr}. Should I check back later instead?",
        "Hi {first}, no pressure on {addr} at all. Would it be better if I tried you in a few months? {sender}",
        "Hey {first}, it's {sender}. Is there someone else I should be asking about {addr}?",
    ], [
        "Hi, {sender} here. If this isn't the owner of {addr}, could you let me know so I can fix my notes?",
        "Hey there, no pressure about {addr}. Is there a better person for me to ask? {sender}",
    ]),
    # The breakup says "for a while", never "ever" and never a promise: mail keeps going, and a
    # later lane may text this owner again.
    4: ([
        "Hi {first}, I'll give {addr} a rest and stop texting for a while. Will you keep my number in case? {sender}",
        "Hey {first}, {sender} here. This is my last note about {addr} for a while. You can text me anytime.",
        "Hi {first}, I'll step back on {addr} after this one. If selling ever makes sense, I'd love to hear. {sender}",
        "Hey {first}, closing my file on {addr} for the time being. Wishing you the best, my line stays open. {sender}",
    ], [
        "Hi, {sender} here. I'll stop texting about {addr} for a while. This number reaches me if the owner wants to talk.",
        "Hey there, last note from me about {addr} for a while. Thanks for your patience! {sender}",
    ]),
}
# Decision D21: never name the list, and for a sale inside two weeks allow wording about OUR speed
# that still does not name it. These replace texts 2 and 3 for a record carrying the urgent tag.
# They speak of how fast we answer and whose date it is, never of why a date might matter.
URGENT = {
    2: ([
        "Hi {first}, {sender} again. If {addr} is yours and timing matters, I can move quickly. Open to a call today?",
        "Hey {first}, I can give you a fast answer on {addr} this week. Want me to call you? {sender}",
        "Hi {first}, {sender} here. If you'd like to sell {addr} soon, I can work on your schedule. Can we talk today?",
        "Hey {first}, if a quick sale of {addr} is on your mind, I can move this week. Should I call? {sender}",
    ], [
        "Hi, {sender} again about {addr}. If timing matters to the owner, I can move quickly. Could we talk today?",
        "Hey there, I can give a fast answer on {addr} this week. Is the owner open to a call? {sender}",
    ]),
    3: ([
        "Hi {first}, I buy as-is and work around the date you pick. Worth a quick call about {addr} today? {sender}",
        "Hey {first}, {sender} here. No repairs or showings on {addr}, and I answer fast. Want the details?",
        "Hi {first}, on {addr} I skip the listing and move on your timing, soon or later. Can we talk today? {sender}",
        "Hey {first}, simple on {addr}: as-is, no agent fees, your date. Is today good for a short call? {sender}",
    ], [
        "Hi, {sender} here. I buy as-is and can move quickly if that suits the owner of {addr}. Could we talk?",
        "Hey there, on {addr} I can skip the listing and work around your date. Worth a quick call? {sender}",
    ]),
}
# The afternoon text (plan 1.3): one short message to a number that was dialed today and not
# reached, in the thread the owner already has. Every line is true by construction: the number was
# dialed today, and "again" is true because the thread exists.
AFTERNOON = ([
    "Hi {first}, {sender} here. I tried calling you today about {addr}. Is there a better time to reach you?",
    "Hey {first}, sorry I missed you earlier today. When's a good time for a quick call about {addr}? {sender}",
    "Hi {first}, it's {sender}. I tried your phone today about {addr}. Would later this week be better?",
    "Hey {first}, {sender} again. I called earlier about {addr} and didn't catch you. Is texting easier?",
], [
    "Hi, {sender} here. I tried calling today about {addr}. Is there a better time to reach the owner?",
    "Hey there, I called earlier today about {addr} and missed you. Is texting easier? {sender}",
])

# The worst case a real record produces (the 99th percentile the Priority 1 templates were sized
# on), so a length check is honest: first name and street together run 35 characters.
WORST = {"first": "Christopher", "addr": "12345 Old Maryville Pike", "sender": "Adriana"}
SEGMENT = 160
OPT_OUT_RESERVE = 27          # room for the platform's opt-out footer, should it be switched back on
TEMPLATE_CAP = SEGMENT - OPT_OUT_RESERVE
SIMILAR = 0.90                # Smarter Contact refused a template at 0.95 similarity and took one at 0.88
TITLE_CAP = 35                # Smarter Contact's limit on a template title
PLACEHOLDERS = {"first", "addr", "sender"}
SC_TAGS = {"first": "<First Name>", "addr": "<Address>"}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Mirrors sms_agent.respond._LIST_WORDS: the seller should feel found, not targeted.
_LIST = re.compile(
    r"\b(foreclos\w*|auction\w*|probate|decedent|inherit\w*|estate sale|tax (lien|sale|delinq\w*)|"
    r"delinquen\w*|liens?|code violation|condemn\w*|evict\w*|divorce|bankrupt\w*|"
    r"behind on (your )?(payment|mortgage|taxes)|pre-?foreclosure|trustee sale|distress\w*|default|"
    r"sale date|court|notice|my list|a list|the list)\b", re.I)
# Words that name the owner's situation or their money (the Priority 1 template rule).
_SITUATION = re.compile(
    r"\b(credit|income|debt|owe\w*|equity|paid off|free and clear|absentee|vacant|landlord|tenants?|"
    r"situation|going through|hard time|struggl\w*|cash offer|we buy houses|any condition|deadline)\b", re.I)
# Smarter Contact refuses a template holding one of these when it is created.
_PLATFORM = re.compile(
    r"\b(irs|abuser?s?|harass\w*|gangs?|now|free|white-?lists?|loans?|lenders?|offers?|winners?|"
    r"limited access|911|grass|smoke|crack|weed|church)\b", re.I)
_PROMISE = re.compile(r"\b(I promise|never (text|bother)|last (one|text|note) (ever|from me)(?! (about [^.?!]* )?for)|"
                      r"one last time|stop texting after)\b", re.I)
_VOICE = re.compile(r"\b(reach out|circle back|touch base|feel free to|do ?n[o']?t hesitate|opportunity|solution|"
                    r"no obligation|act now|limited time|absolutely|certainly)\b", re.I)
_LINK = re.compile(r"(https?://|www\.|\.com\b|\.net\b|\.org\b)", re.I)
_SHOUT = re.compile(r"!{2,}|\b[A-Z]{4,}\b")
_PLAIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,!?'-:{}")
_DASHES = (chr(0x2014), chr(0x2013))


def copy_problems(text: str, *, named: bool, role: str = "ask") -> list[str]:
    """Every reason one variant must not ship. role: "ask" (exactly one question), "breakup" (at
    most one), "thread" (a one-off in an open thread: one question, a full segment)."""
    out = []
    used = set(_PLACEHOLDER.findall(text))
    if used - PLACEHOLDERS:
        out.append("unknown placeholder %s" % sorted(used - PLACEHOLDERS))
        return out
    if not named and "first" in used:
        out.append("greets by first name in a pool for owners with no usable first name")
    if named and "first" not in used:
        out.append("a named variant that does not use the first name")
    if "sender" not in used:
        out.append("not signed: the owner cannot tell who to call back")
    if "addr" not in used:
        out.append("does not name the street")
    if any(d in text for d in _DASHES):
        out.append("an em or en dash")
    if ";" in text:
        out.append("a semicolon")
    if any(ch.isdigit() for ch in text) or "$" in text:
        out.append("a number or a dollar amount in the copy")
    if _LINK.search(text):
        out.append("a link")
    for rx, label in ((_LIST, "names the list"), (_SITUATION, "names the owner's situation"),
                      (_PLATFORM, "a word the texting platform refuses"), (_PROMISE, "promises an ending the lane breaks"),
                      (_VOICE, "does not read like a person"), (_SHOUT, "shouting")):
        m = rx.search(text)
        if m:
            out.append("%s: %s" % (label, m.group(0)))
    stray = sorted({c for c in text if c not in _PLAIN})
    if stray:
        out.append("characters that re-encode the message and double its cost: %s" % stray)
    q = text.count("?")
    if role == "breakup":
        if q > 1:
            out.append("%d questions (at most one)" % q)
    elif q != 1:
        out.append("%d questions (exactly one easy question)" % q)
    cap = SEGMENT if role == "thread" else TEMPLATE_CAP
    n = len(text.format(**WORST))
    if n > cap:
        out.append("%d characters at the worst case (cap %d)" % (n, cap))
    return out


def variants() -> list[tuple[str, str, bool, str]]:
    """(where, text, named, role) for every variant this module owns."""
    out = []
    for step, (named, noname) in FOLLOWUP.items():
        role = "breakup" if step == max(FOLLOWUP) else "ask"
        out += [("follow-up %d named[%d]" % (step, i), t, True, role) for i, t in enumerate(named)]
        out += [("follow-up %d no-name[%d]" % (step, i), t, False, role) for i, t in enumerate(noname)]
    for step, (named, noname) in URGENT.items():
        out += [("urgent text %d named[%d]" % (step, i), t, True, "ask") for i, t in enumerate(named)]
        out += [("urgent text %d no-name[%d]" % (step, i), t, False, "ask") for i, t in enumerate(noname)]
    out += [("afternoon named[%d]" % i, t, True, "thread") for i, t in enumerate(AFTERNOON[0])]
    out += [("afternoon no-name[%d]" % i, t, False, "thread") for i, t in enumerate(AFTERNOON[1])]
    return out


def to_sc(text: str, sender: str) -> str:
    """Neutral placeholders to Smarter Contact's merge tags, the sender as a literal first name."""
    return _PLACEHOLDER.sub(lambda m: sender if m.group(1) == "sender" else SC_TAGS[m.group(1)], text)


_SC_MODULE: list = []


def sc_templates():
    """The existing Smarter Contact template module (read only), or None when this box cannot load
    it. It supplies the Priority 1 touches texts 1 to 3 are sent with, and a second opinion on the
    copy below; nothing here depends on it being present."""
    if not _SC_MODULE:
        try:
            import smarter_contact_templates as mod
            _SC_MODULE.append(mod)
        except Exception as exc:  # noqa: BLE001
            log.info("the Smarter Contact template module is not loadable here (%s)", str(exc)[:120])
            _SC_MODULE.append(None)
    return _SC_MODULE[0]


_AUDIT: list = []


def audit(fresh: bool = False) -> list[str]:
    """Every problem in every variant, plus near-duplicates: Smarter Contact refuses a template too
    close to one already in the account, so each variant is compared with every other one here and
    with the templates the account already holds. Empty means the copy may ship."""
    if _AUDIT and not fresh:
        return list(_AUDIT[0])
    found = []
    rows = variants()
    seen: dict[str, str] = {}
    for where, text, named, role in rows:
        if text in seen:
            found.append("%s: the same text as %s" % (where, seen[text]))
        seen.setdefault(text, where)
        found += ["%s: %s" % (where, p) for p in copy_problems(text, named=named, role=role)]
    for name, pools, floor_named, floor_noname in (("follow-up", FOLLOWUP, 4, 2), ("urgent", URGENT, 4, 2)):
        for step, (named, noname) in pools.items():
            if len(named) < floor_named or len(noname) < floor_noname:
                found.append("%s %d: needs %d named and %d no-name variants" % (name, step, floor_named, floor_noname))
    rendered = [(where, to_sc(text, WORST["sender"])) for where, text, _, _ in rows]
    others = []
    mod = sc_templates()
    if mod is not None:
        try:
            others = [mod.to_sc(t, WORST["sender"]) for _, _, nm, nn in mod._arms() for t in nm + nn]
            others += [mod.to_sc(t, WORST["sender"]) for _, _, nm, nn in mod.land_arms() for t in nm + nn]
            for where, text, _, role in rows:
                key = "4" if role == "breakup" else "2"
                for p in mod.problems(text, kind="reply" if role == "thread" else "touch", key=key):
                    if "chars at the worst case" in p and role == "thread":
                        continue
                    found.append("%s: %s" % (where, p))
        except Exception as exc:  # noqa: BLE001  (a second opinion that fails must not pass for a clean one)
            found.append("the existing template rules could not be run over the copy (%s)" % str(exc)[:120])
    for i, (where, text) in enumerate(rendered):
        close = max([_similar(text, x) for x in others] + [_similar(text, x) for j, (_, x) in enumerate(rendered) if j != i] + [0.0])
        if close >= SIMILAR:
            found.append("%s: %.2f similar to another template (the texting platform refuses near-duplicates)" % (where, close))
    for title in [t["title"] for t in templates(WORST["sender"])]:
        if len(title) > TITLE_CAP:
            found.append("template title %r is over %d characters" % (title, TITLE_CAP))
    found = list(dict.fromkeys(found))
    _AUDIT[:] = [found]
    return list(found)


def _similar(a: str, b: str) -> float:
    """How alike two templates are, 0 to 1, or 0.0 when they cannot reach the refusal line: the two
    cheap upper bounds are asked first, so the full comparison runs only on a pair that might."""
    m = difflib.SequenceMatcher(None, a.lower(), b.lower())
    if m.real_quick_ratio() < SIMILAR or m.quick_ratio() < SIMILAR:
        return 0.0
    return m.ratio()


def template_title(template_set: str, step: int, audience: str, i: int) -> str:
    aud = "No Name" if audience == "noname" else "Named"
    if template_set == "ftm_followup":
        return "FTM F%d %s %02d" % (step, aud, i)
    if template_set == "ftm_urgent":
        return "FTM Urgent T%d %s %02d" % (step, aud, i)
    return "P1 T%d %s %02d" % (step, aud, i)


def templates(sender: str) -> list[dict]:
    """Every template this module owns, in the form Smarter Contact stores (merge tags, the sender
    as a literal name), for whoever creates them there. The afternoon text is not a template: it
    is typed into an open thread, one owner at a time."""
    out = []
    for template_set, pools in (("ftm_followup", FOLLOWUP), ("ftm_urgent", URGENT)):
        for step, (named, noname) in pools.items():
            for audience, pool in (("named", named), ("noname", noname)):
                for i, text in enumerate(pool, 1):
                    out.append({"title": template_title(template_set, step, audience, i), "set": template_set, "step": step,
                                "audience": audience, "content": to_sc(text, sender),
                                "worst_case_chars": len(text.format(**dict(WORST, sender=sender)))})
    return out


def template_titles(template_set: str, step: int, audience: str) -> list[str]:
    """The template titles a send uses. For the Priority 1 set the count comes from the existing
    module when it loads; without it the list is empty and the load names only the touch and the
    audience, which is all the launcher needs to find them."""
    if template_set == "ftm_followup":
        pool = FOLLOWUP[step][1 if audience == "noname" else 0]
    elif template_set == "ftm_urgent":
        pool = URGENT[step][1 if audience == "noname" else 0]
    elif template_set != "p1":
        return []            # a template set this module does not know: the load names it, the launcher finds it
    else:
        mod = sc_templates()
        if mod is None or step not in getattr(mod, "TOUCHES", {}):
            return []
        _, named, noname, _ = mod.TOUCHES[step]
        pool = noname if audience == "noname" else named
    return [template_title(template_set, step, audience, i) for i in range(1, len(pool) + 1)]


def render_afternoon(seed: str, first: str, street: str, sender: str, turn: int = 0) -> str:
    """One afternoon text. The variant is picked by the record and moves on by one each day, so an
    owner who is dialed three days running never reads the same sentence twice."""
    pool = AFTERNOON[0] if first else AFTERNOON[1]
    base = int(hashlib.sha256(("afternoon|%s" % seed).encode()).hexdigest(), 16)
    text = pool[(base + int(turn)) % len(pool)].format(first=first, addr=street, sender=sender)
    return " ".join(text.split())


# ---------------------------------------------------------------------- who is greeted by name
def no_name_reason(owner: dict) -> str:
    """Why this owner cannot be greeted by first name, or "". The same rule the Priority 1 split
    uses (smarter_contact_templates.no_name_reason): Smarter Contact prints the RAW first-name
    field and cannot branch on it, so a company, a set of initials, several words or all capitals
    go to the no-name templates."""
    mod = sc_templates()
    if mod is not None:
        try:
            return mod.no_name_reason(owner)
        except Exception:  # noqa: BLE001
            pass
    fn = " ".join(str(owner.get("first_name") or "").split())
    ln = " ".join(str(owner.get("last_name") or "").split())
    co = " ".join(str(owner.get("company") or "").split())
    if not fn:
        return "no first name (company or trust)" if co else "no first name"
    try:
        from sms_agent.knowledge import touches
        if touches.is_entity(" ".join(p for p in (fn, ln) if p) or co):
            return "entity words in the name"
        if not touches.clean_first(("%s %s" % (fn, ln)).strip()):
            return "initials only"
    except Exception:  # noqa: BLE001
        if len(fn.replace(".", "")) < 2:
            return "initials only"
    if " " in fn:
        return "first-name field holds several words"
    if fn.isupper() and len(fn) > 1:
        return "first name in all caps"
    return ""


def greeting_name(owner: dict) -> str:
    """The first name a text rendered here may greet with, or "" (the no-name wording is used
    then). Never the surname of an owner known only by initials, never a company."""
    if no_name_reason(owner):
        return ""
    fn = " ".join(str(owner.get("first_name") or "").split())
    ln = " ".join(str(owner.get("last_name") or "").split())
    try:
        from sms_agent.knowledge.touches import clean_first
        return clean_first(("%s %s" % (fn, ln)).strip())
    except Exception:  # noqa: BLE001
        return fn.title() if fn.isalpha() and len(fn) > 1 else ""


def contact_values(rec: dict) -> tuple[str, str, dict]:
    """(first name, last name, the contact's fields by field KEY) for a Smarter Contact contact, in
    the shape the land loader builds (smarter_contact_land.contact_input): a company goes in the
    last-name slot with no first name, and the record's id rides in the Sift Record field so a
    reply finds its record."""
    ow = rec.get("owner") or {}
    a, m = rec.get("address") or {}, ow.get("address") or {}
    company = (ow.get("company") or "").strip()
    first = "" if company else (ow.get("first_name") or "").strip()
    last = company or (ow.get("last_name") or "").strip()
    vals = {"ADDRESS": (a.get("street") or "").strip(), "CITY": a.get("city") or "", "STATE": a.get("state") or "",
            "ZIP": str(a.get("postal_code") or "")[:5],
            "MAILING_ADDRESS": m.get("street") or "", "MAILING_CITY": m.get("city") or "",
            "MAILING_STATE": m.get("state") or "", "MAILING_ZIP": str(m.get("postal_code") or "")[:5],
            "SIFT_RECORD": rec.get("uuid") or ""}
    return first, last, {k: v for k, v in vals.items() if v}


def contact_input(contact: dict, keys: dict) -> dict:
    """One load contact as Smarter Contact's CreateContactInput. `keys` maps a field key to the
    field id in that account (the loader reads it live; the Sift Record custom field under
    SIFT_RECORD), exactly as the land loader does it."""
    return {"phone": "+1" + contact["phone"], "firstName": contact.get("first_name") or "", "lastName": contact.get("last_name") or "",
            "attrs": [{"fieldId": keys[k], "value": v} for k, v in (contact.get("values") or {}).items() if v and k in keys]}


def plan_rows(load: dict, keys: dict) -> list[dict]:
    """A released load as the rows the land loader's create phase works from: one per number, with
    the group it joins and the contact to create. A number already in Smarter Contact (it is in the
    owner's thread) is listed with new_contact False: it only joins its new group."""
    return [{"lane": c.get("lane") or load.get("lane") or "", "group": c["group"], "phone": c["phone"], "record": c["record"],
             "new_contact": bool(c.get("new_contact")), "input": contact_input(c, keys)} for c in load.get("contacts") or []]


# ---------------------------------------------------------------------- days and groups
def _mmdd(day: date) -> str:
    return "%02d%02d" % (day.month, day.day)


def add_business_days(day: date, n: int) -> date:
    d = day
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def cohort_name(label: str, day: date, audience: str, group_tz: str) -> str:
    """The day's new group for one audience and one sending window: the thread its numbers keep."""
    return "%s %s %s %s" % (label, _mmdd(day), "No Name" if audience == "noname" else "Named", "East" if group_tz == "east" else "Other TZ")


def last_open_day(lanes_cfg, lane: str, channel: str, counter: int) -> int:
    """The last lane day on which the table still opens a channel, asked of the table itself so no
    day is written down twice. -1 when it never opens."""
    last = -1
    for d in range(0, 260):
        if lanes.channel_open(lanes_cfg, lane, channel, d, counter, None, 0)[0]:
            last = d
    return last


def followup_plan(tl: dict, lanes_cfg, lane: str) -> dict:
    """The follow-up series as the table writes it: how many, how far apart, when it closes."""
    fu = tl.get("followup_texts") or {}
    count = int(fu.get("count", 0) or 0)
    ends = int(tl.get("round_ends_at", 5))
    if count <= 0:
        return {"count": 0}
    first, last = int(fu.get("first_day", 8)), int(fu.get("last_day", fu.get("first_day", 8)))
    span = max(0, last - first)
    offsets = [round(i * span / (count - 1)) for i in range(count)] if count > 1 else [0]
    gap = max(1, span // (count - 1)) if count > 1 else 0
    return {"count": count, "first": first, "last": last, "offsets": offsets, "gap": gap, "earliest": min(first, ends),
            "close": last_open_day(lanes_cfg, lane, "followup_text", ends)}


# ---------------------------------------------------------------------- the stage
ROOM_WORDS = {
    "day": "no room: the owner already has a text today",
    "week": "no room: the owner's texts for the week are used",
    "platform": "not yet: the texting platform is resting the contact",
    "thread": "no room: the owner's text thread is on another texting system",
    "month": "left for next month (the month's texts are used)",
    "capacity": "left for tomorrow (today's texts are used)",
    "hours": "outside the sending hours",
    "registry": "no number left to text (do-not-call registry)",
    "owner": "the record names no owner",
}


def block_problems(st) -> list[str]:
    """What is wrong with a text stage's playbook block, in the playbook checker's words. A typo in
    `series` must fail `cli.py autopilot check`, not wait for the first morning run to say so."""
    out = []
    series = str(st.get("series", "opening")).strip().lower()
    if series not in SERIES:
        out.append("stage %s: series must be opening or followup, not %r" % (st.id, st.get("series")))
    if not st.get("lane"):
        out.append("stage %s: a text stage needs the lane whose timeline it reads" % st.id)
    many = st.get("lanes")
    if many is not None:
        bad = [x for x in (many if isinstance(many, list) else [many]) if str(x) not in lanes.ORDER]
        if not isinstance(many, list) or bad:
            out.append("stage %s: lanes must list lanes (%s); %s is not one" % (st.id, ", ".join(lanes.ORDER), bad or many))
        elif st.get("lane") and str(st.get("lane")) not in [str(x) for x in many]:
            out.append("stage %s: lanes must include the stage's own lane %s" % (st.id, st.get("lane")))
    labels = st.get("labels")
    if labels is not None and not isinstance(labels, dict):
        out.append("stage %s: labels must map a lane to its group name prefix" % st.id)
    if series == "opening" and not st.presets:
        out.append("stage %s: the opening texts need the presets that hold the lane's new records" % st.id)
    for key, low in (("numbers_per_owner", 1), ("daily_owners", 0), ("max_records", 1), ("max_new", 1)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    v = st.get("text_usd")
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
        out.append("stage %s: text_usd must be what one text costs, in dollars" % st.id)
    return out


def _label(st, lane: str) -> str:
    """The Smarter Contact group name prefix for a lane: its entry under `labels`, else the stage's
    `label` for the stage's own lane, else the lane's name. Two lanes in one stage get two prefixes,
    so a group's results can be read for first to market and for the Top 1,000 apart."""
    labels = st.get("labels") if isinstance(st.get("labels"), dict) else {}
    if labels.get(lane):
        return str(labels[lane]).strip()
    if st.get("label") and lane == str(st.get("lane") or ""):
        return str(st.get("label")).strip()
    return lane.upper().replace("_", " ").strip()


def _lane_words(lanes_list: list[str]) -> str:
    names = [lanes.name(x) for x in lanes_list]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _street(rec: dict) -> str:
    a = (rec or {}).get("address") or {}
    return (a.get("street") or a.get("full_address") or "").strip() if isinstance(a, dict) else str(a)


def _drawn(ctx: Ctx, lane: str) -> set | None:
    """For a lane worked in tranches (the stage block sets `tranche`, build plan 4.1): every record
    that was ever drawn into one. The presets are already cut to the current tranche by stage_rows;
    the follow-up series reads the whole lane instead of a preset, so it applies the rule itself: a
    record still waiting behind the tranches is never texted. A record from an earlier tranche keeps
    its follow-ups. None means the stage is not restricted. A stage that asks for tranches on a
    build with no tranche table loads nothing: everything at once is what the setting prevents."""
    if not ctx.stage.get("tranche"):
        return None
    members = getattr(lanes, "tranche_members", None)
    if members is None:
        ctx.out.problem("the stage works in tranches and this build has no tranche table; the lane scan loaded nothing")
        return set()
    return set(members(lane))


def _candidates(ctx: Ctx, series: str, work: list[str], lanes_cfg) -> list[tuple[str, dict]]:
    """(where it came from, search row) for every record the series could concern, each row carrying
    its lane under `_lane`. The opening texts work the presets that hold new records (stage_rows
    reads a two-lane stage one lane at a time, first to market first). The follow-up series works
    every record that carries a lane's marker and is not finished for good, plus any preset the
    block names: by then a record has left the call presets, and a mailed one sits in no mail
    preset for a month."""
    st = ctx.stage
    out, seen = [], set()
    if st.presets:
        for title, row in stage_rows(ctx, extra=required_tags(ctx)):
            if row.get("uuid") not in seen:
                seen.add(row["uuid"])
                out.append((title, row if row.get("_lane") else dict(row, _lane=work[0])))
    elif series == "opening":
        ctx.out.problem("the opening texts need the presets that hold the lane's new records; none are named")
    if series == "followup" and st.get("scan_lane", True):
        for lane in work:
            marks = (lanes_cfg.markers.get(lane) or {}).get("tags") or []
            ids = [ctx.crm.tag_uuid(t) for t in marks]
            drawn = _drawn(ctx, lane)
            if ids and all(ids):
                for row in ctx.crm.rows({"any_tags": ids, "must_not": {"any_property_status": GONE_STATUSES}},
                                        cap=int(st.get("max_scan", 3000))):
                    if not row.get("uuid") or row["uuid"] in seen:
                        continue
                    if drawn is not None and row["uuid"] not in drawn:
                        ctx.out.tally["behind the lane's tranches, waiting their turn"] += 1
                        continue
                    seen.add(row["uuid"])
                    out.append(("lane", dict(row, _lane=lane)))
            elif marks:
                ctx.out.problem("the lane's marker tag %s does not exist in this account, so the follow-up series has "
                                "nobody to read in the %s lane" % (marks, lane.replace("_", " ")))
    return out


def expire_stale(ctx: Ctx, today: str) -> None:
    """A text batch is released on its day or not at all: the lane days, the limits and the sending
    window it was built for are yesterday's. Yesterday's unreleased batch of this stage is closed
    here and its owners are free to be picked up again."""
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        full = ledger.batch(b["id"])
        if not full or full["kind"] != KIND:
            continue
        day = next((it.get("day") for it in full["items"] if it.get("day")), "")
        if not day or day >= today:
            continue
        nice = date.fromisoformat(day).strftime("%b %d").replace(" 0", " ")
        ctx.out.problem("batch %d (%d owner(s), staged for %s) was never released, so nothing was sent; those owners are picked up again" % (
            b["id"], len(full["items"]), nice))
        if ctx.live:
            limits.drop_text(b["id"], status="expired")
            ledger.batch_set(b["id"], "expired", {"why": "not released on %s" % day})


def stage_batch(ctx: Ctx, items: list[dict], usd: float) -> int:
    """Stage one batch of texts for a person to release, and hold each owner's place for the day in
    the text ledger. Staging writes only the autopilot's own tables; nothing leaves the box."""
    bid = ledger.batch_create(ctx.stage.id, KIND, items, usd)
    for it in items:
        limits.stage_text(stage=ctx.stage.id, batch_id=bid, owner_uuid=it["owner_uuid"], record_uuid=it["record_uuid"],
                          numbers=it["numbers"], system=it["system"], series=it["series"], step=it["step"], touch=it["touch"],
                          tz=ctx.policy.tz, campaign=bool(it.get("campaign", True)),
                          usd_each=float(it.get("price") or 0) / max(1, len(it["numbers"])), when=ctx.now)
    return bid


def pull(batch_id: int, record_uuids, commit: bool = False) -> int:
    """Take these records out of a staged text batch before anyone releases it, for the revocation
    stage (an owner who opted out, died, sold or became a lead on Tuesday must not sit in Monday's
    batch). Returns how many texts were pulled. With commit the batch is rewritten and those texts
    stop holding the owners' place in the ledger; without it nothing changes. The release would
    hold these owners back anyway: this only makes the batch a person sees match what will go."""
    b = ledger.batch(int(batch_id))
    if not b or b["kind"] != KIND or b["status"] != "staged":
        return 0
    gone_ids = set(record_uuids or ())
    keep = [it for it in b["items"] if it.get("record_uuid") not in gone_ids]
    gone = [it for it in b["items"] if it.get("record_uuid") in gone_ids]
    if gone and commit:
        ledger.batch_items_set(int(batch_id), keep, sum(float(it.get("price") or 0) for it in keep))
        for it in gone:
            limits.drop_text(int(batch_id), it["record_uuid"])
    return sum(len(it.get("numbers") or []) for it in gone)


def _facts(keys: list[str]) -> dict:
    """key -> {record uuid: value} for a few remembered facts, read in one query. A lane's presets
    can hold thousands of rows, and asking for each record's facts one at a time would open the
    database that many times before the first record is even looked at."""
    from .. import store
    out: dict[str, dict] = {k: {} for k in keys}
    if not keys:
        return out
    with store._LOCK, store._db() as c:
        q = "SELECT record_uuid, key, value FROM ap_facts WHERE key IN (%s)" % ",".join("?" * len(keys))
        for r in c.execute(q, list(keys)).fetchall():
            try:
                out[r["key"]][r["record_uuid"]] = json.loads(r["value"])
            except ValueError:
                out[r["key"]][r["record_uuid"]] = r["value"]
    return out


def _pick(numbers: list[dict], limit: int) -> list[dict]:
    """Best first: Dial First, then Dial Second, then a number a person confirmed whatever its
    tier; at most `limit` of them (the Priority 1 loads averaged three numbers an owner)."""
    ranked = sorted(numbers, key=lambda n: (TIER_RANK.get(n.get("tier") or "", 2),
                                            0 if n.get("status") in ("CORRECT", "NO_ANSWER") else 1))
    out, seen = [], set()
    for n in ranked:
        if n["number"] not in seen:
            seen.add(n["number"])
            out.append(n)
    return out[:max(1, int(limit))]


def _lane_plan(lanes_cfg, lane: str, series: str) -> dict | None:
    """One lane's part of the series: its timeline, the day window the series runs in, how many texts
    and, for the follow-ups, their spacing. None when the lane has no such texts. Each lane of a
    two-lane stage is planned from its own row of the lanes table, never from the stage's."""
    tl = lanes_cfg.timeline(lane) if lane else None
    if not tl:
        return None
    if series == "opening":
        steps_total = len(tl.get("opening_texts") or [])
        window = (min(tl.get("opening_texts") or [0]), last_open_day(lanes_cfg, lane, "text", 0))
        fplan = {}
    else:
        fplan = followup_plan(tl, lanes_cfg, lane)
        steps_total = fplan["count"]
        window = (fplan.get("earliest", 0), fplan.get("close", -1))
    if steps_total <= 0 or window[1] < 0:
        return None
    return {"tl": tl, "steps_total": steps_total, "window": window, "fplan": fplan}


def run_textlane(ctx: Ctx) -> None:
    st = ctx.stage
    series = str(st.get("series", "opening")).strip().lower()
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    if series not in SERIES:
        ctx.out.problem("series must be opening or followup, not %r; nothing was loaded" % series)
        return
    work = stage_lanes(st)
    if not work or not any(lanes_cfg.timeline(ln) for ln in work):
        ctx.out.problem("the text lane works a lane with a written timeline; set `lane` in the stage block (for example ftm)")
        return
    lim, bad = limits.read(ctx.policy)
    for p in bad:
        ctx.out.problem(p)
    copy_bad = audit()
    if copy_bad:
        ctx.out.problem("the text copy fails its own audit (%d problem(s), the first: %s); nothing was loaded" % (
            len(copy_bad), copy_bad[0]))
        return
    tz = ctx.policy.tz
    zone = ZoneInfo(tz)
    today = ctx.now.astimezone(zone).date()
    today_s = today.isoformat()
    what = "an opening text" if series == "opening" else "a follow-up text"
    if today.weekday() not in ctx.policy.days:
        ctx.out.head = "%s is not a sending day." % today.strftime("%A")
        return
    expire_stale(ctx, today_s)
    if not limits.zones_known():
        ctx.out.problem("the area code table did not load on this box, so every number goes in the late group and no "
                        "owner's own clock is checked")
    if sc_templates() is None:
        ctx.out.tally["template titles not listed in the load (the template module did not load on this box)"] += 1

    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    system = str(st.get("system", "smarter_contact"))
    per_owner = int(st.get("numbers_per_owner", 3))
    text_usd = float(st.get("text_usd", 0.02))
    stage_cap = st.get("daily_usd")
    max_owners = int(st.get("daily_owners", 150))
    max_reads = int(st.get("max_records", 150))
    max_new = int(st.get("max_new", 150))
    require_screen = bool(st.get("require_screen", True))
    stop_on_correct = bool(st.get("stop_on_correct", True))
    urgent_tag = str(st.get("urgent_tag", "Auction Critical")).strip().lower() if st.get("urgent_copy", True) else ""
    # The sale-date copy (D21) belongs to a courthouse notice: a Top 1,000 house has no sale date,
    # so only these lanes may get it whatever their tags say.
    urgent_lanes = {str(x) for x in (st.get("urgent_lanes") or ["ftm"])}
    lead = timedelta(minutes=float(st.get("lead_minutes", 30)))
    allow = st.get("gate_allow") or ()
    # Texts 1 to 3 go out on the templates the account already holds: the Priority 1 touches, which
    # acknowledge the noise and never the situation. A lane with its own opening copy names its set.
    opening_set = str(st.get("opening_set", "p1")).strip() or "p1"

    from . import ftmpipeline
    plans = {}
    for ln in work:
        p = _lane_plan(lanes_cfg, ln, series)
        if p is None:
            ctx.out.tally["the %s lane has no %s on its timeline" % (
                ln.replace("_", " "), "opening texts" if series == "opening" else "follow-up texts")] += 1
            continue
        p["label"] = _label(st, ln)
        # A lane worked on the live First to Market board takes its day from the card's column
        # (ftmpipeline.text_slot), not from the calendar since the record landed.
        p["board"] = bool(ftmpipeline.follows(ctx, ln))
        plans[ln] = p
    if not plans:
        ctx.out.head = "The %s lane's timeline has no %s." % (
            _lane_words(work), "opening texts" if series == "opening" else "follow-up texts")
        return

    rows = [(t, r) for t, r in _candidates(ctx, series, work, lanes_cfg) if r.get("_lane") in plans]
    ctx.out.found = len(rows)
    for title, ln in dict.fromkeys((t, r["_lane"]) for t, r in rows):
        scrub.note_rows([r for t, r in rows if t == title and r["_lane"] == ln], ln, ctx.now)   # every flag the rows show is remembered first
    staged = ledger.staged_record_uuids(st.id)
    room_left = limits.capacity_left(ctx.now, lim, tz)
    spent_today = limits.spent(today_s, st.id)
    items: list[dict] = []
    owners_in: set = set()
    numbers_in: set = set()
    usd = 0.0
    messages = 0
    reads = new_reads = 0
    by_step: Counter = Counter()
    tally = ctx.out.tally
    series_words = "opening texts" if series == "opening" else "follow-up texts"
    # On the board the series closes by the card's column, so it is remembered under its own key: a
    # series closed by the calendar before the board went live does not stop the board's texts.
    keys = {ln: (CLOSED_FACT % (series, ln) + (":board" if plans[ln]["board"] else ""), UNKNOWN_FACT % ln,
                 "lane_entry:%s" % ln) for ln in plans}
    facts = _facts(sorted({k for trio in keys.values() for k in trio} | {"death_screen"}))

    # Order of work, decided from what is already remembered and with no request made: lane by lane,
    # first to market first, and inside a lane the records whose lane day is known first, then
    # records never looked at, then the ones whose log did not show an entry date last time. A
    # backlog of old records can then never use up the run's reads before today's new records are
    # seen, and no Top 1,000 owner takes a first-to-market owner's place under the day's caps.
    queue = []
    for ln in plans:
        closed_key, unknown_key, entry_key = keys[ln]
        known, fresh, again = [], [], []
        for title, row in rows:
            if row["_lane"] != ln:
                continue
            u = row.get("uuid") or ""
            if not u or u in facts[closed_key]:
                continue
            if u in facts[entry_key]:
                known.append((title, row))
                continue
            tried = facts[unknown_key].get(u)
            if not isinstance(tried, dict):
                fresh.append((title, row))
            elif ctx.now - datetime.fromisoformat(tried["at"]) >= UNKNOWN_RETRY:
                again.append((title, row))
            else:
                tally["lane entry date not known yet (the log is asked again tomorrow)"] += 1
        queue += known + fresh + again

    for title, row in queue:
        u = row.get("uuid") or ""
        owner = (row.get("owner") or {}).get("uuid") or ""
        lane = row["_lane"]
        plan_ = plans[lane]
        tl, window, steps_total, fplan, label = plan_["tl"], plan_["window"], plan_["steps_total"], plan_["fplan"], plan_["label"]
        closed_key, unknown_key, entry_key = keys[lane]
        if u in staged:
            tally["already staged, waiting on release"] += 1
            continue
        rv = gate.check_row(row, "text", cfg=gate_cfg, allow=allow)
        if not rv.ok:
            tally["held back: %s" % rv.reason()] += 1
            continue
        if owner and owner in owners_in:
            tally["the owner's other record is already in today's load"] += 1
            continue
        if len(owners_in) >= max_owners:
            tally["left for tomorrow (daily cap %d owners)" % max_owners] += 1
            continue
        if messages >= room_left:
            tally[ROOM_WORDS["capacity"]] += 1
            continue
        slot = ftmpipeline.text_slot(ctx, u, lane, series, today) if plan_.get("board") else None
        if slot is not None and slot.get("hold"):
            tally[slot["hold"]] += 1
            continue
        if slot is not None:
            # The card's column says which day of the round it is: the round follows the full
            # attempts, so a card held a day keeps its text, and no log read is needed.
            entered = date.fromisoformat(slot["since"]) if slot.get("since") else today
            day = int(slot["day"])
            if series == "opening" and owner:
                had = {int(t["step"]) for t in limits.touches(owner, today_s, since_day=entered.isoformat(), series=series)}
                if had and max(had) >= int(slot.get("max_step") or 0) > 0:
                    # Asked before the record is opened: a card held for its call costs no read.
                    tally["the text for the card's day went already: the card waits on its call"] += 1
                    continue
        else:
            # Which day of the lane is it for this record? The first look reads the record's log once;
            # after that the answer is remembered, and a record whose series is over is never read again.
            if u not in facts[entry_key]:
                if new_reads >= max_new:
                    tally["not looked at yet (read cap, next run)"] += 1
                    continue
                new_reads += 1
            entered_at = lane_entry(ctx, row, lane, resolve=False)     # the lane is known: the row came from its own pass
            if entered_at.year <= 2000:
                # The log does not show when the record entered the lane. When a log read failed in this
                # run that proves nothing about the record, so it is not counted against it; and when
                # the failure was the hour's request budget, reading stops here with what is built.
                log_failed = [p for p in ctx.out.problems if "could not read a record's log" in p]
                if log_failed:
                    tally["lane entry date not read (the log did not answer)"] += 1
                    if any("for this hour" in p or "free slot" in p for p in log_failed):
                        ctx.out.problem("stopped reading early: this hour's share of DataSift requests is used; what was built so far is kept")
                        break
                    continue
                # It has been in the lane longer than the log reaches. Ask again on a later day, and
                # after three such answers take it as old.
                n = int((facts[unknown_key].get(u) or {}).get("n", 0)) + 1
                if n >= UNKNOWN_TRIES:
                    ledger.set_fact(u, closed_key, {"why": "in the lane longer than the log reaches",
                                                    "at": ctx.now.isoformat(timespec="seconds")})
                    tally["%s closed for the record (in the lane longer than the log reaches)" % series_words] += 1
                else:
                    ledger.set_fact(u, unknown_key, {"n": n, "at": ctx.now.isoformat(timespec="seconds")})
                    tally["lane entry date not known yet (the log is asked again tomorrow)"] += 1
                continue
            entered = entered_at.astimezone(zone).date()
            day = lanes.business_days(entered, today)
        if day > window[1]:
            ledger.set_fact(u, closed_key, {"day": day, "at": ctx.now.isoformat(timespec="seconds")})    # knowledge, kept in plan mode too
            tally["%s closed for the record (past day %d of the lane)" % (series_words, window[1])] += 1
            continue
        if day < window[0]:
            tally["not yet: the %s start on day %d" % (series_words, window[0])] += 1
            continue
        if require_screen and u not in facts["death_screen"]:
            if ledger.fact(u, SCREEN_PLAN_HIT, max_age=timedelta(hours=24)) is not None:
                tally["possible death: waiting on research"] += 1
            else:
                tally["waiting on the death screen"] += 1
            continue
        if owner and limits.touches(owner, today_s, since_day=today_s):
            tally[ROOM_WORDS["day"]] += 1
            continue
        if reads >= max_reads:
            tally["not read yet (read cap, next run)"] += 1
            continue
        reads += 1
        try:
            rec = ctx.crm.record(u)
        except budget.BudgetExhausted as exc:
            # The hour's share of requests is used. Stop reading, and stage what is already built:
            # losing the morning's whole load to the last record read would cost every owner a day.
            ctx.out.problem("stopped reading early: %s; what was built so far is kept" % exc)
            break
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not read a record (%s)" % str(exc)[:100])
            continue
        v = ctx.verdict(rec, "text", row=row, lane=lane)
        if not v.ok:
            if v.code == "no_number" and scrub.shown(row)[1] and gate.usable_numbers(rec, "text", gate_cfg, None):
                # The gate found nothing to text only because the one usable number is the number
                # the registry flags: counted as the registry's doing, so the lane's loss is honest.
                tally[ROOM_WORDS["registry"]] += 1
            else:
                tally["held back: %s" % v.reason()] += 1
            continue
        ow = rec.get("owner") or {}
        owner = ow.get("uuid") or owner
        if not owner:
            tally[ROOM_WORDS["owner"]] += 1
            continue
        if owner in owners_in:          # asked again here: a search row does not always name the owner
            tally["the owner's other record is already in today's load"] += 1
            continue
        counter = int(rec.get("predictivecall_attempts") or 0)
        if slot is not None:
            counter = int(slot.get("counter", counter))     # the full attempts the board counted
        ok, why = lanes.channel_open(lanes_cfg, lane, "text" if series == "opening" else "followup_text", day, counter,
                                     None, today.weekday())
        if not ok:
            tally["closed today: %s" % why] += 1
            continue
        if stop_on_correct and any(str(p.get("status") or "").upper() == "CORRECT" for p in (ow.get("phones") or [])):
            tally["the owner already answered: a person has the thread"] += 1
            continue
        limits.observe(owner, rec, ctx.now, tz, lim)
        keep, removed = scrub.split(v.numbers, "text", ctx.policy, row, lane, ctx.now)
        if removed:
            tally["numbers left out: on the do-not-call registry"] += len(removed)
        if not keep:
            tally[ROOM_WORDS["registry"]] += 1
            continue

        entered_s = entered.isoformat()
        mine = limits.touches(owner, today_s, since_day=entered_s, series=series)
        done_steps = {int(t["step"]) for t in mine}
        threads = limits.number_threads(owner, since_day=entered_s)
        first_contact = not threads
        if series == "opening":
            step = (max(done_steps) + 1) if done_steps else 1
            if step > steps_total:
                tally["finished the opening texts"] += 1
                continue
            if slot is not None and step > int(slot.get("max_step") or 0):
                # Day N allows text N. A card held for its call already had the day's text.
                tally["the text for the card's day went already: the card waits on its call"] += 1
                continue
            if step > 1:
                keep = [n for n in keep if n["number"] in threads]
                if not keep:
                    tally["no number left on the owner's thread"] += 1
                    continue
        else:
            if steps_total in done_steps:
                ledger.set_fact(u, closed_key, {"day": day, "at": ctx.now.isoformat(timespec="seconds")})
                tally["finished the follow-up texts"] += 1
                continue
            anchor_key = ANCHOR_FACT % lane + (":board" if slot is not None else "")
            anchor = ledger.fact(u, anchor_key)
            if not isinstance(anchor, int):
                anchor = day
                ledger.set_fact(u, anchor_key, anchor)       # the day the series opened for the record: knowledge
            k = (max(done_steps) + 1) if done_steps else 1
            if day < anchor + fplan["offsets"][min(k, steps_total) - 1]:
                tally["not yet: the next follow-up is not due"] += 1
                continue
            if mine:
                if slot is not None and slot.get("base_date"):
                    last_day = int(slot["base_day"]) + lanes.business_days(date.fromisoformat(slot["base_date"]),
                                                                           date.fromisoformat(mine[-1]["day"]))
                else:
                    last_day = lanes.business_days(entered, date.fromisoformat(mine[-1]["day"]))
                if day - last_day < fplan["gap"]:
                    tally["not yet: %d business days between follow-ups" % fplan["gap"]] += 1
                    continue
            # The series always ends with the breakup: when no later text fits before it closes,
            # this one is the last.
            step = steps_total if (k >= steps_total or day + fplan["gap"] > fplan["close"]) else k
            if not first_contact:
                keep = [n for n in keep if n["number"] in threads]
                if not keep:
                    tally["no number left on the owner's thread"] += 1
                    continue
        # One phone is one contact: a number already in today's texts under another owner is left out.
        shared = [n for n in keep if n["number"] in numbers_in or limits.number_busy(n["number"], today_s, owner)]
        if shared:
            tally["numbers left out: already in today's texts for another owner"] += len(shared)
            keep = [n for n in keep if n not in shared]
            if not keep:
                continue
        keep = _pick(keep, per_owner)

        placed = []
        for n in keep:
            group_tz, at, why = limits.planned_send(n["number"], today, lim, tz, not_before=ctx.now + lead)
            if at is None:
                left_out = "today's sending window has closed" if "closed" in why else "outside the owner's daytime"
                tally["number left out: %s" % left_out] += 1
                continue
            placed.append(dict(n, tz=group_tz, at=at))
        if not placed:
            continue
        r = limits.room(owner, "text", min(p["at"] for p in placed), policy=ctx.policy, lim=lim, tz=tz, system=system, campaign=True)
        if not r.ok:
            tally[ROOM_WORDS.get(r.code, "no room: %s" % r.why)] += 1
            continue
        price = round(len(placed) * text_usd, 4)
        if stage_cap is not None and spent_today + usd + price > float(stage_cap):
            tally["left for tomorrow (stage cap $%.2f a day)" % float(stage_cap)] += 1
            continue
        if messages + len(placed) > room_left:
            tally[ROOM_WORDS["capacity"]] += 1
            continue

        audience = "noname" if no_name_reason(ow) else "named"
        tags = {t.lower() for t in tag_titles(rec)}
        if series == "followup":
            template_set, touch_no = (opening_set, 1) if first_contact else ("ftm_followup", step)
        elif urgent_tag and urgent_tag in tags and step in URGENT and lane in urgent_lanes:
            template_set, touch_no = "ftm_urgent", step
        else:
            template_set, touch_no = opening_set, step
        numbers = []
        for p in placed:
            mine_thread = threads.get(p["number"])
            base = cohort_name(label, today, audience, p["tz"])
            if series == "opening" and step == 1:
                group, cohort = base, base
            elif series == "opening":
                cohort = (mine_thread or {}).get("thread", "").split(":", 1)[-1] or base
                group = "%s T%d" % (cohort, step) + (" Urgent" if template_set == "ftm_urgent" else "")
                offs = list(tl.get("opening_texts") or [])
                due = add_business_days(date.fromisoformat((mine_thread or {}).get("day") or today_s), offs[step - 1] - offs[0])
                if due != today:
                    group += " Late %s" % _mmdd(today)
            else:
                group = "%s F%d %s" % (label, step, base[len(label) + 1:])
                cohort = (mine_thread or {}).get("thread", "").split(":", 1)[-1] or group
            numbers.append({"number": p["number"], "tier": p.get("tier") or "", "status": p.get("status") or "",
                            "tz": p["tz"], "send_at": limits._iso(p["at"]), "group": group, "cohort": cohort,
                            "thread": "%s:%s" % (system, cohort), "new_contact": mine_thread is None})
        first, last, values = contact_values(rec)
        street = _street(rec)
        items.append({"record_uuid": u, "owner_uuid": owner, "street": street, "day": today_s, "lane": lane, "lane_day": day,
                      "series": series, "step": step, "touch": "%s:%s:%s:%d:%s" % (st.id, owner, series, step, today_s),
                      "system": system, "campaign": True, "audience": audience, "template_set": template_set,
                      "template_touch": touch_no, "templates": template_titles(template_set, touch_no, audience),
                      "first_name": first, "last_name": last, "values": values, "numbers": numbers, "price": price,
                      "sms_before": int(rec.get("sms_attempts") or 0), "text": ""})
        owners_in.add(owner)
        numbers_in.update(n["number"] for n in numbers)
        usd += price
        messages += len(numbers)
        by_step[step] += 1
        ctx.out.line("%s: %s %d to %d number(s) ending %s" % (
            street or u[:8], "text" if series == "opening" else "follow-up", step, len(numbers),
            ", ".join(n["number"][-4:] for n in numbers)))

    name = "text" if series == "opening" else "follow-up"
    mix = ", ".join("%d on %s %d" % (n, name, s) for s, n in sorted(by_step.items()))
    if len(plans) > 1:
        per = Counter(it["lane"] for it in items)
        mix += "; " + ", ".join("%d %s" % (per[ln], lanes.name(ln)) for ln in plans if per.get(ln))
    if not items:
        ctx.out.head = "No owner in the %s lane is due %s today." % (_lane_words(list(plans)), what)
        return
    ctx.out.acted = len(items)
    if not ctx.live:
        ctx.reserve(usd)
        ctx.out.head = "Would stage %d text(s) to %d owner(s), about $%.2f: %s. Nothing is sent until a person releases the batch." % (
            messages, len(items), usd, mix)
        return
    bid = stage_batch(ctx, items, usd)
    ctx.out.batch = bid
    ctx.out.head = ("Staged batch %d: %d text(s) to %d owner(s), about $%.2f (%s). Nothing is sent until a person releases it: "
                    "python src/agents/cli.py autopilot release %d --commit" % (bid, messages, len(items), usd, mix, bid))


# ---------------------------------------------------------------------- release
def _row_for(crm, owner_uuid: str, record_uuid: str) -> dict | None:
    """The record's search row, the only place the registry flag shows. Every row the owner has is
    remembered on the way, so a flag on the owner's other record counts here too."""
    try:
        rows = crm.owner_records(owner_uuid, cap=25) if owner_uuid else []
    except Exception:  # noqa: BLE001
        return None
    scrub.note_rows(rows)
    return next((r for r in rows if r.get("uuid") == record_uuid), None)


def load_path(batch_id: int) -> Path:
    return Path(config.DATA_DIR) / "texts" / ("batch_%d.json" % int(batch_id))


def build_load(batch: dict, kept: list[dict], held: list[dict], now: datetime, tz: str) -> dict:
    """The released batch as the load a texting-side tool takes: the groups to create, the contacts
    to put in them (one per number), and who was held back and why."""
    groups: dict[str, dict] = {}
    contacts = []
    for it in kept:
        for n in it["numbers"]:
            if n.get("group"):       # a one-off in an open thread joins no group: it goes to the thread itself
                g = groups.setdefault(n["group"], {
                    "name": n["group"], "series": it["series"], "step": it["step"], "audience": it["audience"], "tz": n.get("tz") or "",
                    "template_set": it.get("template_set") or "", "touch": it.get("template_touch") or it["step"],
                    "templates": it.get("templates") or [], "start": n.get("send_at") or "", "contacts": 0})
                g["contacts"] += 1
                g["start"] = min(g["start"], n.get("send_at") or g["start"]) if g["start"] else (n.get("send_at") or "")
            contacts.append({"group": n["group"], "cohort": n.get("cohort") or "", "thread": n.get("thread") or "",
                             "lane": it.get("lane") or "",
                             "phone": n["number"], "record": it["record_uuid"], "owner": it["owner_uuid"],
                             "series": it["series"], "step": it["step"], "new_contact": bool(n.get("new_contact")),
                             "first_name": it.get("first_name") or "", "last_name": it.get("last_name") or "",
                             "values": it.get("values") or {}, "text": it.get("text") or "", "send_by": it.get("send_by") or ""})
    first = kept[0] if kept else {}
    # One batch can hold two lanes (the Priority track): each contact carries its own, and `lanes`
    # names them all; `lane` stays the first item's for a loader written before two-lane batches.
    return {"batch": batch["id"], "stage": batch["stage"], "lane": first.get("lane") or "",
            "lanes": sorted({it.get("lane") or "" for it in kept} - {""}), "series": first.get("series") or "",
            "system": first.get("system") or "", "campaign": bool(first.get("campaign", True)),
            "day": first.get("day") or limits._day(now, tz), "released": limits._iso(now),
            "groups": sorted(groups.values(), key=lambda g: g["name"]), "contacts": contacts, "held": held}


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, now: datetime | None = None, uploaded: bool = False) -> dict:
    """A person releases a staged text batch. Every owner is checked again first (the gate, the
    registry rule, the reply mark, the limits and the sending hours): someone who opted out, died,
    sold, became a lead or answered since the batch was staged gets nothing. What passes is written
    to a load file for the texting side, counted in the text ledger, and the record's text counter
    is raised by one. Dry unless commit; a dry release reads and reports and writes nothing to the
    account. This function hands the load over. It does not send."""
    from . import policy as policy_mod
    b = ledger.batch(int(batch_id))
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    now = now or datetime.now(timezone.utc)
    tz = pol.tz
    today = limits._day(now, tz)
    items = b["items"]
    day = next((it.get("day") for it in items if it.get("day")), today)
    if day != today:
        raise SystemExit("batch %d was staged for %s and today is %s: a text batch is released on its day or not at all. "
                         "Run the stage again for today's load." % (batch_id, day, today))
    if crm is None:
        from .crm import CRM
        crm = CRM()
    lim = limits.settings(pol)
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=commit)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now, live=commit, out=out)
    stop_on_correct = bool(st.get("stop_on_correct", True))
    kept, held, why_count = [], [], Counter()
    recs: dict[str, dict] = {}

    def hold(it: dict, why: str) -> None:
        why_count[why] += 1
        held.append({"record": it["record_uuid"][:8], "street": it.get("street") or "", "why": why})

    for it in items:
        u, owner = it["record_uuid"], it["owner_uuid"]
        try:
            rec = crm.record(u)
        except Exception as exc:  # noqa: BLE001
            hold(it, "the record could not be read (%s)" % str(exc)[:60])
            continue
        row = _row_for(crm, owner, u)
        v = ctx.verdict(rec, "text", row=row, lane=it.get("lane") or "")
        if not v.ok:
            hold(it, v.reason())
            continue
        phones = (rec.get("owner") or {}).get("phones") or []
        if stop_on_correct and any(str(p.get("status") or "").upper() == "CORRECT" for p in phones):
            hold(it, "the owner answered since the batch was staged: a person has the thread")
            continue
        limits.observe(owner, rec, now, tz, lim)
        usable = {n["number"] for n in scrub.split(v.numbers, "text", pol, row, it.get("lane") or "", now)[0]}
        nums, closed = [], 0
        for n in it["numbers"]:
            if n["number"] not in usable:
                continue
            when = max(datetime.fromisoformat(n["send_at"]), now)
            ok, _ = limits.inside_hours(lim.text_hours, when, tz, n["number"], "text")
            if not ok:
                closed += 1
                continue
            nums.append(dict(n, send_at=limits._iso(when)))
        if it.get("send_by") and now > datetime.fromisoformat(it["send_by"]):
            hold(it, "past the time the afternoon texts go out by: they are not sent late")
            continue
        if not nums:
            hold(it, "the sending window has closed for today" if closed else "no number left that a text may use")
            continue
        r = limits.room(owner, "text", min(datetime.fromisoformat(n["send_at"]) for n in nums), policy=pol, lim=lim, tz=tz,
                        system=it.get("system") or "", campaign=bool(it.get("campaign", True)), exclude_batch=int(batch_id))
        if not r.ok:
            hold(it, r.why)
            continue
        recs[u] = rec
        kept.append(dict(it, numbers=nums))

    texts = sum(len(it["numbers"]) for it in kept)
    if not commit:
        return {"batch": int(batch_id), "dry": True, "owners": len(kept), "texts": texts, "of": len(items),
                "would_hold": dict(why_count)}

    # The load file is written first. If it cannot be written nothing has been counted or changed
    # yet, and the batch is still staged; the other order would count texts nobody was handed.
    load = build_load(b, kept, held, now, tz)
    path = ""
    if kept:
        p = load_path(batch_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(load, indent=1, default=str), encoding="utf-8")
        path = str(p)
    state: dict = {}
    bump = bool(st.get("bump_counter", True))
    for it in kept:
        u = it["record_uuid"]
        if bump and not state.get("broken"):
            cur = int(recs[u].get("sms_attempts") or 0)
            ctx.change(u, "sms_attempts", cur, cur + 1)
            try:
                ctx.write("text counter", crm.sms_attempts, u, cur + 1)
                if not state.get("verified"):
                    state["verified"] = True
                    back = int(crm.record(u).get("sms_attempts") or 0)
                    if back != cur + 1:
                        state["broken"] = True
                        out.problem("the text counter did not stick on %s (wrote %d, read %d); the rest of this batch skips "
                                    "the counter, and the texts are still counted in the autopilot's own ledger" % (
                                        it.get("street") or u[:8], cur + 1, back))
            except Exception as exc:  # noqa: BLE001  (the load is already handed over: the ledger must still record all of it)
                state["broken"] = True
                out.problem("the text counter could not be written on %s (%s); the rest of this batch skips the counter, "
                            "and the texts are still counted in the autopilot's own ledger" % (it.get("street") or u[:8], str(exc)[:100]))
            if not state.get("broken"):
                limits.own_bump(u, cur + 1, now)
        limits.release_text(int(batch_id), u, [n["number"] for n in it["numbers"]])
        ledger.event(ctx.run_id, st.id, u, "text", {"series": it["series"], "step": it["step"], "numbers": len(it["numbers"]),
                                                    "system": it.get("system") or "", "est_usd": it.get("price") or 0},
                     now=now, tz=tz)
    limits.drop_text(int(batch_id))          # whatever is still staged in this batch was held back
    ledger.batch_set(int(batch_id), "sent" if kept else "held",
                     {"owners": len(kept), "texts": texts, "held": dict(why_count), "load": path, "problems": out.problems})
    said = ("Handed over, not sent: the texting side takes the load file from here." if kept
            else "Nothing passed the checks, so nothing was handed over.")
    return {"batch": int(batch_id), "run": ctx.run_id, "owners": len(kept), "texts": texts, "of": len(items),
            "held": dict(why_count), "load": path, "problems": out.problems, "next": said}
