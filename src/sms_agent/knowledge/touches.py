"""The four-touch outreach pools.

Copied from the `text-touch-builder` skill's message recipe so the agent and
the skill send the same proven copy. Keep them in sync: if you rewrite a
variant here, rewrite it there.

Variant selection is seeded by the record itself, so neighbouring records get
different sequences (the cold-email rotation principle) and re-running produces
identical text for a record that already exists.
"""
from __future__ import annotations

import hashlib
import re

# Touch 1 verifies identity. Touch 2 resends. Touch 3 asks softly. Touch 4 says
# goodbye. Never combine jobs.
TOUCH1 = [
    "Hi {first}! I hope your week is going great. My name is {sender}, I was looking at {addr} and was wondering if it's yours? Thanks so much!",
    "Hi {first}, I pray all is well your way! I'm {sender}, and I know this is random, but does {addr} happen to be yours? Do I have the right person?",
    "Hey {first}, I hope you are doing great! I'm not even sure I have the right number, but is {addr} yours? Thank you! {sender}",
    "Hi there! I hope things are going well for you. This is {sender}, hoping to speak with {first} about {addr}. Do I have the right number?",
    "Hi {first}! My name is {sender}. I've been looking at {addr} in {city} and was wondering, does it belong to you by any chance? Have a great day!",
]
TOUCH1_NONAME = [
    "Hi! I hope your week is going great. My name is {sender}, I'm trying to reach the owner of {addr}. Did I get the right number? Thanks so much!",
    "Hi there! This is {sender}. I know this is random, but I'm hoping to reach whoever handles {addr} in {city}. Do I have the right contact?",
]
TOUCH2 = [
    "Hi {first}, I reached out the other day and wasn't sure my text went through. Is {addr} your place? {sender} here.",
    "Hey, sorry to bother you! Did you get my message about {addr}? Just want to make sure I have the right contact. I'm {sender}.",
    "Hi {first}! {sender} again. Sometimes my texts don't go through, so I wanted to try once more. Is {addr} yours?",
    "Hey {first}, just floating my last text back up in case it got buried. Is {addr} your property? Thanks! {sender}",
]
TOUCH2_NONAME = [
    "Hi, {sender} here again. I texted the other day about {addr} and wasn't sure it went through. Is this the right contact for that property?",
    "Hey, sorry to double text! Did my message about {addr} come through? Just making sure I have the right contact. I'm {sender}.",
]
TOUCH3 = [
    "Hi {first}, {sender} again about {addr}. If it's yours, have you ever thought about selling it? No pressure at all, just curious!",
    "Hey {first}! I hope I'm not being a bother. I'm interested in {addr} and would love to ask you a couple quick questions. Would a short call work?",
    "Hi {first}, this is {sender}. I work with homeowners in {city} and I'd love to chat about {addr} for a minute or two. Would you be open to that?",
    "Hey {first}, me again! If you've ever considered an offer on {addr}, I'd love to be the one you talk to first. Can I give you a quick call?",
]
TOUCH3_NONAME = [
    "Hi, {sender} again. If {addr} is one of yours, would you be open to a quick conversation about it? Happy to work around your schedule!",
    "Hey there, this is {sender}. I'm interested in {addr} in {city}. If you handle that property, would a short call sometime work for you?",
]
TOUCH4 = [
    "Hi {first}, I've sent a few texts about {addr} and haven't heard back. Did you decide to keep it instead? Either way, wishing you the best! {sender}",
    "Hey {first}, last one from me, I promise! If selling {addr} is ever on your mind, I'd love to be your first call. Take care! {sender}",
    "Hi {first}, I'll stop bugging you after this! Just wanted to leave my number in case {addr} ever becomes something you'd like to talk about. {sender}",
    "Hey {first}, {sender} here one more time. If I have the wrong number, I'm so sorry! If not, I'd still love to connect about {addr} whenever works for you.",
]
TOUCH4_NONAME = [
    "Hi, {sender} here one last time about {addr}. If there's a better contact for that property, I'd be grateful for a point in the right direction. Thanks!",
    "Hey there, last text from me! If {addr} is ever something you'd consider selling, I'd love to be your first call. All the best! {sender}",
]

POOLS = [
    (TOUCH1, TOUCH1_NONAME),
    (TOUCH2, TOUCH2_NONAME),
    (TOUCH3, TOUCH3_NONAME),
    (TOUCH4, TOUCH4_NONAME),
]

# "Who is this?" is the most common reply to touch 1, and the playbook already
# prescribes the answer: warm intro, describe yourself by LOCALITY, name the
# street, ask one qualifying question, aim at a call.
#
# Two hard rules here (Ty, 2026-08-11):
#   * NEVER name the company. A named business in a cold text is litigation
#     bait, so identity is always "a small local team" plus the record's own
#     county or city.
#   * Do not re-introduce as though it is a first contact. The sender's first
#     name is already in touch 1, so this answers the question they asked and
#     moves, rather than restarting the conversation.
#
# Written to survive the same validator as everything else: no dollar figure,
# no link, no zip, one question mark, under 320 characters.
WHO = [
    "Hi {first}! It's {sender}. Sorry for the random text! I'm with a small local team here in {place} that buys a few houses a year, and I came across {addr}. Would you ever consider an offer on it?",
    "Hey {first}, {sender} here. I should have led with that! I'm a local buyer around {place} and {addr} is one that caught my eye. Have you ever thought about selling it?",
    "Hi {first}! {sender} here, and fair question. I'm part of a small local team that buys homes here in {place}. I saw {addr} and figured I would ask. Is it something you would ever sell?",
    "Hey {first}, it's {sender}. Sorry, I know that came out of the blue! I buy a few houses around {place} each year and {addr} stood out to me. Would you be open to an offer on it?",
]
WHO_NONAME = [
    "Hi! It's {sender}. Sorry for the random text! I'm with a small local team here in {place} that buys a few houses a year, and I came across {addr}. Would you ever consider an offer on it?",
    "Hey, {sender} here. I should have led with that! I'm a local buyer around {place}, and {addr} is one that caught my eye. Have you ever thought about selling it?",
]


def render_who(seed: str, first: str, addr: str, place: str, sender: str) -> str:
    """The answer to "who is this?", rotated per record like the touches."""
    pool = WHO if first else WHO_NONAME
    idx = int(hashlib.sha256(f"who|{seed}".encode()).hexdigest(), 16) % len(pool)
    return pool[idx].format(
        first=first, sender=sender, addr=addr, place=place or "the area"
    )

# Entities never get "Hi FirstName". A trust does not have a first name.
#
# County records abbreviate relentlessly, and the abbreviations are what bite.
# "Willis Ailene B Life Est" is a LIFE ESTATE; the un-abbreviated pattern missed
# it and produced "Hey Willis" on a live preview. Anything carrying a legal
# status token is an entity, not a person to greet by name.
ENTITY_RX = re.compile(
    r"\b("
    r"llc|l\.?l\.?c|inc|corp|co|company|lp|llp|pllc|ltd|"
    r"trust|trustee|ttee|tr|"
    r"estate|est|l/e|etal|et\s*al|heirs?|"
    r"properties|property|holdings|investments|ventures|partners|"
    r"bank|assoc|association|church|ministries|"
    r"authority|district|dept|department|housing|"
    r"deceased|dec'?d|survivor"
    r")\b"
    r"|\b(city|state|county)\s+of\b",
    re.I,
)


def _is_name_token(token: str) -> bool:
    return len(token) >= 2 and token.replace("'", "").replace("-", "").isalpha()


def clean_first(raw: str) -> str:
    """The usable first name, or empty.

    "C Eugene Suthard" gives "Eugene". "E A Henry" gives NOTHING, which routes
    to the owner-of-the-address wording instead of "Hi Henry!".

    That second case is the subtle one. Taking "the first token of length two
    or more" walks past the initials and lands on the SURNAME, so an
    initials-only owner gets greeted by their last name. The fix is positional:
    on a multi-token name, only the tokens BEFORE the surname can supply a
    first name. If they are all initials, we do not have one.
    """
    tokens = (raw or "").replace(".", " ").split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].title() if _is_name_token(tokens[0]) else ""
    for token in tokens[:-1]:  # everything except the surname
        if _is_name_token(token):
            return token.title()
    return ""


def is_entity(owner_full: str) -> bool:
    return bool(ENTITY_RX.search(owner_full or ""))


def render(touch: int, seed: str, first: str, addr: str, city: str, sender: str) -> str:
    """Render one touch (1-4). `seed` makes selection deterministic per record."""
    if not 1 <= touch <= 4:
        raise ValueError("touch must be 1-4")
    pool, noname = POOLS[touch - 1]
    chosen = noname if not first else pool
    n = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    template = chosen[(n // (7 ** (touch - 1))) % len(chosen)]
    text = template.format(first=first, addr=addr, city=city or "the area", sender=sender)
    return re.sub(r"\s+", " ", text).strip()
