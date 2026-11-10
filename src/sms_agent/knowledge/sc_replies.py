"""Approved auto-replies for Smarter Contact threads, in Adriana's voice.

Ty, 2026-10-27: every reply is from Adriana's perspective, a local person seeing
whether homeowners might be interested in selling. Nothing critical goes out: no
company name, no price or offer, nothing about the owner's situation, nothing
about how we found them beyond public property records, nothing legal.

Fixed, human-approved copy rather than model-written text, for two reasons.
Smarter Contact's Developer Terms keep its main-terms duty of human review for
software-generated messages but allow sends from a human-approved template; and
a fixed pool cannot invent a price, a company or a promise.

An interested reply gets NO auto-reply at all: it is Adriana's to take over.
A no, STOP, hostility and sensitive replies (a death, a lawyer) get none either.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

# {first} owner first name, {addr} street line, {city} city, {sender} signer.
WHO = [
    "Sorry, should have said! It's {sender}. I'm local and texting a few homeowners around {city} "
    "to see if anyone might sell. Is {addr} yours?",
    "Hi, sorry about that! I'm {sender}, just a local person checking if any homeowners near {city} might "
    "want to sell. Would you ever sell {addr}?",
]
# "How'd you get my number", "what company", or a second "who" (Ty, 2026-10-30:
# "I'm just a local investor. We get the information from public record and
# work with the county directly." Never a company name; keep it broad).
HOW_NUMBER = [
    "Totally fair question! I'm {sender}, just a local investor. We get our info from public records and work "
    "with the county directly. Would you ever think about selling {addr}?",
    "Good question! I'm just a local investor here in {city}. We get the info from public records and work "
    "with the county directly. Is {addr} yours?",
]
# The same question after they already said no (Ty, 2026-10-30: "How you get
# this number who are you?" right after a "No" still gets answered). Answers and
# signs off; no question, because asking about selling again after a no is pushy.
HOW_CLOSING = [
    "Totally fair question! I'm {sender}, just a local investor. We get our info from public records and work "
    "with the county directly. Sorry for the bother, have a great day!",
    "Good question! I'm {sender}, a local investor here in {city}. We get the info from public records and work "
    "with the county directly. Sorry to bug you, have a good one!",
]
# A reply the agent cannot read ("?????", "Why", "I'm not Franklin. What about
# the property"): the setter explains once and asks one easy question, so
# Adriana only takes the ones who are interested (Ty, 2026-10-30).
CLARIFY = [
    "Sorry for the confusion! I'm {sender}, a local investor. I was reaching out to see if the owners of {addr} "
    "would ever think about selling. Are you connected to the property?",
    "Sorry, I should have explained! I'm {sender}, a local investor, and I was seeing if the owners of {addr} "
    "would ever want to sell. Is it yours?",
]
# Copy that has gone out and since been replaced, kept so a thread that already
# got it is not sent the same answer again.
RETIRED = {
    "HOW_NUMBER": ["Fair question! I look through public property records for homes around {city}. Totally "
                   "fine if it's a bad time. Is {addr} yours?"],
}
OWNER_YES = [
    "Thanks for getting back to me, {first}! Would you ever think about selling {addr} if the price made sense?",
    "Appreciate you letting me know, {first}! Is selling {addr} something you'd ever consider?",
]
OWNER_YES_NONAME = [
    "Thanks for getting back to me! Would you ever think about selling {addr} if the price made sense?",
]
WRONG_NUMBER = [
    "So sorry about that, I'll take your number off my list. Have a great day!",
]

# The Smarter Contact label each disposition sets (titles of the account's own
# labels; the DataSift integration maps them to a status or phone status).
# INTERESTED is deliberately absent: "Lead" flips the record to New Lead, which
# fires the assignment sequence, and only the person who calls decides that
# (Ty, 2026-08-11). Adriana labels Lead herself.
LABELS = {
    "NOT_INTERESTED": "Not interested",
    "WRONG_NUMBER": "Wrong Number",
    "OPT_OUT": "Unknown DNC",
}
# A no gets no text back either (Ty, 2026-10-28): the number is marked verified and that is all.
NO_REPLY_INTENTS = {"INTERESTED", "OPT_OUT", "ESCALATE", "NOT_INTERESTED"}

_HOW_NUMBER_RX = re.compile(r"\bhow\b.{0,30}\b(get|got|find|found)\b.{0,30}\b(number|info|me|this)\b"
                            r"|\bwhere\s+did\s+you\s+get\b|\bcompany\b|\bwho\s+do\s+you\s+(work\s+for|represent)\b",
                            re.I)
_FIRM_NO_RX = re.compile(r"\b(never|stop|no means no|leave me alone|don't text|do not text|not ever)\b", re.I)


def asks_how(body: str) -> bool:
    """How did you get my number, what company, who do you work for."""
    return bool(_HOW_NUMBER_RX.search(body or ""))


def is_firm_no(body: str) -> bool:
    return bool(_FIRM_NO_RX.search(body or ""))


def owner_confirmed(body: str) -> bool:
    # The classifier's own rule, so "is this the owner" and "should we answer
    # it" can never disagree.
    from ..classify import OWNER_CONFIRMED, _hit
    return bool(_hit((body or "").strip().lower(), OWNER_CONFIRMED))


def _choose(pool: list[str], seed: str) -> str:
    return pool[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(pool)]


# The answers a thread works through, in order. Each goes out at most once; when
# a thread has had every one that fits, the next reply goes to a person.
_SEQUENCE = {
    "how": ("HOW_NUMBER", "CLARIFY"),
    "who": ("WHO", "HOW_NUMBER", "CLARIFY"),
    "unsure": ("CLARIFY", "HOW_NUMBER"),
}


def _pattern(template: str) -> "re.Pattern":
    # Every fixed piece literally, every {placeholder} as anything. Whole-text,
    # because two pools can open with the same words.
    parts = re.split(r"\{[a-z]+\}", re.sub(r"\s+", " ", template).strip())
    return re.compile(".*?".join(re.escape(p) for p in parts), re.S)


def pool_of(message: str) -> str:
    """Which pool a sent reply came from ('' if none)."""
    for name, pool in list(ALL_POOLS.items()) + list(RETIRED.items()):
        if any(_pattern(t).fullmatch((message or "").strip()) for t in pool):
            return name
    return ""


def pick(intent: str, body: str, prior_replies: int, seed: str, has_first: bool,
         used: frozenset = frozenset(), unsure: bool = False, closing: bool = False) -> Optional[str]:
    """The approved template for this reply, or None when the agent stays quiet.

    `used` is the pools this thread already got; `unsure` is a model read under
    the confidence floor, which gets the clarify answer instead of a guess;
    `closing` is a thread already closed by a no, where only a question about
    who we are gets an answer, and that answer signs off."""
    if intent in NO_REPLY_INTENTS:
        return None
    if closing:
        if (intent == "ASKING_WHO" or asks_how(body)) and "HOW_CLOSING" not in used:
            return _choose(HOW_CLOSING, seed)
        return None
    if intent == "WRONG_NUMBER":
        return _choose(WRONG_NUMBER, seed)
    if intent == "OTHER" and owner_confirmed(body):
        return _choose(OWNER_YES if has_first else OWNER_YES_NONAME, seed)
    if _HOW_NUMBER_RX.search(body or ""):
        order = _SEQUENCE["how"]
    elif intent == "ASKING_WHO" and not unsure:
        order = _SEQUENCE["who"]
    elif unsure and intent in ("ASKING_WHO", "OTHER"):
        order = _SEQUENCE["unsure"]
    else:
        return None  # a clear reply with no approved answer is left alone
    name = next((n for n in order if n not in used), None)
    return _choose(ALL_POOLS[name], seed) if name else None


# Smarter Contact refuses these words in its own templates and carriers filter
# them in any text (measured at template create, 2026-10-21).
_SC_BANNED = re.compile(r"\b(now|free|offer|loan|lender|winner|irs|abuse|harass\w*|gang|whitelist|911|"
                        r"limited access|jew|jewish)\b", re.I)
# GSM-7 basic set only: one character outside it turns the whole text into
# UCS-2 and halves the segment, and the extended set (^{}[]~|\) counts double.
_GSM_EXTENDED = set("^{}[]~|\\")


def problems(message: str) -> list[str]:
    """What is wrong with a rendered reply ([] when it may go out)."""
    found = []
    hit = _SC_BANNED.search(message)
    if hit:
        found.append(f"filtered word {hit.group(0)!r}")
    if any(ord(ch) > 126 or ch in _GSM_EXTENDED for ch in message):
        found.append("character outside plain text (dash, emoji or brace)")
    if len(message) > 320:
        found.append(f"{len(message)} characters")
    if message.count("?") > 1:
        found.append("more than one question")
    return found


def render(template: str, first: str, addr: str, city: str, sender: str) -> str:
    text = template.format(first=first or "", addr=addr or "your property",
                           city=city or "the area", sender=sender)
    return re.sub(r"\s+", " ", text).strip()


ALL_POOLS = {
    "WHO": WHO, "HOW_NUMBER": HOW_NUMBER, "HOW_CLOSING": HOW_CLOSING, "CLARIFY": CLARIFY, "OWNER_YES": OWNER_YES,
    "OWNER_YES_NONAME": OWNER_YES_NONAME, "WRONG_NUMBER": WRONG_NUMBER,
}
