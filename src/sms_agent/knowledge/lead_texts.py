"""Fixed copy for the two texts this app sends that are not outreach and not a conversation:
the "a call is coming" text to a new lead (build plan 2.2) and the line check sent to our own
handset before a campaign day (4.7).

Fixed copy rather than model-written text, for the reason every other pool in this folder is
fixed: a pool cannot invent a price, a company or a promise. The call text makes exactly one
promise, that somebody will call, and the only thing it is allowed to vary is who.

These lines were drafted with the build (2026-11-13) and wait on the owner's read. That is why
the switch that sends them defaults to off, and why its first step stages each text for a
person to release.

Rules the copy follows, each enforced where it is rendered (respond.validate and
sc_replies.problems), not just here:
  * no question. The owner already raised a hand; the next thing they hear is a voice.
  * no price, no company, nothing about the house or how we found it.
  * plain characters only, one segment. Smarter Contact refuses the word "now" (and "free",
    "offer") in a template and carriers filter them, so "in a few minutes", never "right now".
  * a first name only when one is known to be a real first name; otherwise no name at all.
"""
from __future__ import annotations

import hashlib
import re

# {first} the owner's first name, {caller} who will call.
# The person texting is the person calling (the thread is signed by the record's assignee, and
# the handoff pages the same name), so the text speaks in the first person.
CALL_SOON = [
    "Thanks {first}! I will give you a call in just a few minutes.",
    "Thanks for getting back to me, {first}! I will call you in a few minutes.",
    "Appreciate the reply, {first}! Give me a few minutes and I will call you.",
]
CALL_SOON_NONAME = [
    "Thanks for getting back to me! I will give you a call in just a few minutes.",
    "Appreciate the reply! Give me a few minutes and I will call you.",
]
# A lead that came in after hours, sent when the window opens.
CALL_MORNING = [
    "Good morning {first}! Thanks for getting back to me. I will give you a call this morning.",
    "Good morning {first}! I saw your message and will call you this morning.",
]
CALL_MORNING_NONAME = [
    "Good morning! Thanks for getting back to me. I will give you a call this morning.",
    "Good morning! I saw your message and will call you this morning.",
]
# The same four, for when the caller is somebody other than the name on the thread.
OTHER_SOON = [
    "Thanks {first}! {caller} is going to give you a call in just a few minutes.",
    "Appreciate the reply, {first}! {caller} will call you in a few minutes.",
]
OTHER_SOON_NONAME = [
    "Thanks for getting back to me! {caller} is going to give you a call in just a few minutes.",
]
OTHER_MORNING = [
    "Good morning {first}! Thanks for getting back to me. {caller} will give you a call this morning.",
]
OTHER_MORNING_NONAME = [
    "Good morning! Thanks for getting back to me. {caller} will give you a call this morning.",
]

# One text per sending number to OUR OWN handset. It says what it is, because the person holding
# that handset is one of us and may be looking at twenty of them.
CANARY = [
    "Line check. No reply needed.",
]

ALL_POOLS = {
    "CALL_SOON": CALL_SOON, "CALL_SOON_NONAME": CALL_SOON_NONAME, "CALL_MORNING": CALL_MORNING,
    "CALL_MORNING_NONAME": CALL_MORNING_NONAME, "OTHER_SOON": OTHER_SOON, "OTHER_SOON_NONAME": OTHER_SOON_NONAME,
    "OTHER_MORNING": OTHER_MORNING, "OTHER_MORNING_NONAME": OTHER_MORNING_NONAME, "CANARY": CANARY,
}


def _choose(pool: list[str], seed: str) -> str:
    return pool[int(hashlib.sha256(seed.encode()).hexdigest(), 16) % len(pool)]


def render_call(seed: str, first: str = "", caller: str = "", same_person: bool = True, morning: bool = False) -> str:
    """The call-is-coming text. `same_person` is whether the caller is the name the thread was
    signed with; when it is not, the text names the caller, and with no caller name to give it
    returns "" so nothing is sent rather than promise a call from nobody."""
    if not same_person and not (caller or "").strip():
        return ""
    name = ("OTHER_" if not same_person else "CALL_") + ("MORNING" if morning else "SOON") + ("" if first else "_NONAME")
    text = _choose(ALL_POOLS[name], seed).format(first=first or "", caller=(caller or "").strip())
    return re.sub(r"\s+", " ", text).strip()


def render_canary(seed: str = "") -> str:
    return _choose(CANARY, seed or "canary")
