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

# ---------------------------------------------------------------------------
# HEIR TOUCHES. The owner pools above ask "is {addr} yours?", which is the wrong
# question for a relative: the person we are texting is NOT the owner of record,
# and on many of these records the owner has died. So the heir copy asks who
# LOOKS AFTER the property instead of who owns it. That one change carries the
# whole difference.
#
# Everything the owner rules forbid still applies, and two rules matter more
# here than anywhere else:
#   * NEVER reference a death, an estate, probate, inheritance or "your loss".
#     We are frequently wrong about who died: 64% of these relationships are
#     SmartSkip's generic "In-Law"/"Relative" labels, and on 194 records the
#     top relative is a spouse who may well be alive and living there. A text
#     that presumes a death lands as a condolence to someone who has not lost
#     anybody, or worse, tells somebody something they did not know.
#   * Never claim to know their connection. Every variant ASKS.
# The failure mode being designed out is a message that reads as if we have
# been reading somebody's obituary, because that is exactly what we did.
HEIR_TOUCH1 = [
    "Hi {first}! My name is {sender}. I hope your week is going well. I'm trying to find who looks after {addr} these days. Would that be your family?",
    "Hi {first}, I'm {sender} and I know this is out of the blue. Does your family still look after the place on {addr}?",
    "Hey {first}, I hope you're doing well! I'm {sender}. I'm trying to reach whoever takes care of {addr} now. Is that you?",
    "Hi {first}! {sender} here. I'm asking around about {addr} and your name came up. Are you connected to that one?",
    "Hey {first}, hope your day is going well. I'm {sender}, a local buyer. Do you know who looks after {addr} now?",
]
HEIR_TOUCH1_NONAME = [
    "Hi there! My name is {sender}. I'm trying to find who looks after {addr} these days. Am I anywhere close with this number?",
    "Hello! {sender} here, a local buyer. I'm trying to reach whoever takes care of {addr}. Would you know?",
]
HEIR_TOUCH2 = [
    "Hi {first}, I sent a note the other day and wasn't sure it went through. Do you know who handles {addr} now? {sender} here.",
    "Hey {first}, {sender} again. My texts don't always land, so I wanted to try once more. Is your family connected to {addr}?",
    "Hi {first}! Sorry to bother you. Did my message about {addr} come through? Just making sure I have the right person. I'm {sender}.",
    "Hey {first}, {sender} here again. Still trying to work out who looks after {addr}. Any chance you'd know?",
]
HEIR_TOUCH2_NONAME = [
    "Hi, {sender} here again. I wasn't sure my note about {addr} went through. Do you know who looks after it?",
    "Hello! Trying once more in case my text didn't land. Who would I talk to about {addr}? Thanks! {sender}",
]
HEIR_TOUCH3 = [
    "Hi {first}, I'm {sender}, a local buyer here in {city}. If your family ever wanted to sell {addr} as it sits, would you want a number on it?",
    "Hey {first}! {sender} here. I buy houses around {city} and I'd take {addr} exactly as it is, nothing to fix or clean out. Worth a quick chat?",
    "Hi {first}, {sender} again. If {addr} is ever something the family wants to move on, I can make it simple. Would that be worth talking about?",
]
HEIR_TOUCH3_NONAME = [
    "Hi, {sender} here. I'm a local buyer in {city}. If {addr} is ever something your family wants to sell as is, would a number be useful?",
    "Hello! I buy houses around {city} as they sit. If {addr} ever comes up, would you want me to take a look? {sender}",
]
HEIR_TOUCH4 = [
    "Hi {first}, {sender} here. I'll stop texting after this one. If {addr} ever comes up down the road, I'm around. Take care!",
    "Hey {first}, last note from me about {addr}. If anything changes, just text me back. Thanks for your time! {sender}",
    "Hi {first}, {sender} one last time. If I've got the wrong person for {addr}, no worries at all. Wishing you well!",
]
HEIR_TOUCH4_NONAME = [
    "Hi, {sender} here one last time about {addr}. If there's a better person to ask, I'd be grateful for a pointer. Thank you!",
    "Hello, last text from me! If {addr} is ever something your family would sell, I'd love to be the first call. All the best! {sender}",
]

HEIR_POOLS = [
    (HEIR_TOUCH1, HEIR_TOUCH1_NONAME),
    (HEIR_TOUCH2, HEIR_TOUCH2_NONAME),
    (HEIR_TOUCH3, HEIR_TOUCH3_NONAME),
    (HEIR_TOUCH4, HEIR_TOUCH4_NONAME),
]

# ---------------------------------------------------------------------------
# BUYER TOUCHES. This is the dispo side and it inverts the seller program: we
# are not asking whether a house is theirs, we are telling an investor what we
# have and what it costs.
#
# The shape is Ty's (2026-08-28): the ROAD NAME and the AREA, never the exact
# address, plus the PRICE. Interest converts to a phone call, and the agent
# never negotiates.
#
# Three touches, not four. A deal has a short life, so the sequence is drop,
# bump, last call. Touch 3 doubles as buy-box capture: if this one is not their
# kind of deal, asking what is turns a no into a better-targeted next blast.
#
# Every variant still has to clear the same validator as the seller copy, minus
# the money rule: no link, no zip, no emoji, no semicolon, no dash, one question
# mark, under 320 characters. The price is the ONE thing allowed through here
# that is blocked over there, and it is checked against the approved deal sheet.
# Ty, 2026-08-31: say we have A PROPERTY ON the road, and let the offer do
# the redacting. An earlier pass said it defensively ("Not posting the exact
# address here"), which draws attention to the withholding and reads like a
# rule we are enforcing on the buyer. Offering the address as part of the
# package instead ("video walkthrough, photos, and full address if you're
# interested") conveys the same fact, gives the buyer a reason to reply, and
# sounds like a person with a real house rather than a gatekeeper. The road
# name is still the only location stated anywhere.
BUYER_TOUCH1 = [
    "Hi {who}, I saw you've been buying around {city}. We have a property on {road}, {beds}/{baths}, {sqft} sqft, at {price}. I can send over the video walkthrough, photos, and full address if you're interested. -{sender}",
    "Hi {who}, came across you as an active buyer in {city}. We have a property on {road}, {beds}/{baths}, {sqft} sqft, asking {price}. I can send over the video walkthrough, photos, and full address if you're interested. -{sender}",
    "Hey {who}, I saw you buy around {city} so I figured I'd let you know. We have a property on {road} under contract, {beds}/{baths}, {sqft} sqft, at {price}. Happy to send the walkthrough, photos, and full address if you're interested. -{sender}",
]
BUYER_TOUCH2 = [
    "Hi {who}, following up on the property we have on {road} in {city}, {beds}/{baths}, {sqft} sqft at {price}. I can send over the walkthrough, photos, and full address if you're interested. -{sender}",
    "Hey {who}, the property we have on {road} is still around. {beds}/{baths}, {sqft} sqft, {price}. Happy to send the walkthrough, photos, and full address if you want a look. -{sender}",
]
BUYER_TOUCH3 = [
    "Hi {who}, last one from me on the {road} property. It's at {price} and close to gone. If it's not your kind of deal, what should I send you instead? -{sender}",
    "Hey {who}, wrapping up the property we have on {road} at {price} this week. If {city} isn't where you're buying right now, just let me know what is. -{sender}",
]

# ADDRESS-FORWARD pools (Ty, 2026-09-02). Blast 1 named the road and held
# the address back; blast 2 leads with the full address and offers the
# lockbox so a buyer can walk it themselves. Two pools, not one, because the
# two audiences have different histories with us: the 16 who engaged on the
# last deal are told 'I actually have another one at this price', and the
# 131 who never replied get 'we have another one at this price as well'.
# One message for both would either claim a relationship that does not exist
# or waste the one that does.
#
# Neither wording says or implies the previous deal sold.
#
# The lockbox CODE is deliberately not a merge field. The copy offers it;
# a human sends it, because that code is physical access to the house.
DEAL_ADDR_WARM = [
    "Hi {who}, since you looked at the last one, we have another one. {address}, {price}, {beds}/{baths}, {sqft} sqft. I have photos I can send over, and happy to send the lockbox code if you want to walk it. -{sender}",
    "Hi {who}, you looked at the last one so I wanted you to see this one. {address}, {price}, {beds}/{baths}, {sqft} sqft. Happy to send photos over, or the lockbox code if you want to walk it. -{sender}",
    "Hey {who}, we have another one and I thought of you after the last one. {address}, {price}, {beds}/{baths}, {sqft} sqft. I can send photos, and the lockbox code if you want to get in and look. -{sender}",
]
DEAL_ADDR_COLD = [
    "Hi {who}, we have another one. {address}, {price}, {beds}/{baths}, {sqft} sqft. I have photos I can send over, and happy to send the lockbox code if you want to walk it. -{sender}",
    "Hi {who}, we have another one available. {address}, {price}, {beds}/{baths}, {sqft} sqft. Happy to send photos over, or the lockbox code if you want to walk it. -{sender}",
    "Hey {who}, we have another one. {address}, {price}, {beds}/{baths}, {sqft} sqft. I can send photos, and the lockbox code if you want to get in and look. -{sender}",
]
ADDR_POOLS = {"warm": DEAL_ADDR_WARM, "cold": DEAL_ADDR_COLD}

BUYER_POOLS = [BUYER_TOUCH1, BUYER_TOUCH2, BUYER_TOUCH3]

# The validator's hard ceiling. Kept here so render_deal can fit a
# message to it instead of handing the validator one it must reject.
MAX_SMS = 320

# Token sets, not regexes. A backslash escape has been mangled on the way to
# this file three times in one session (a word boundary reaching disk as a
# literal backspace byte), so the name handling is written with plain string
# operations that have nothing to escape.
_LEGAL_SUFFIX = {"LLC", "L.L.C.", "L.L.C", "INC", "INC.", "INCORPORATED",
                 "CORP", "CORP.", "CORPORATION", "CO", "CO.", "LP", "LLP",
                 "PLLC", "LTD", "LTD."}
# ESTATE is NOT a blanket trust marker: "Affordable Houses and Real
# Estate" is a company, and treating it as a trust dropped its "team"
# and its transparency note. It only counts as a trust marker when it
# ENDS the name and is not preceded by "REAL".
_TRUST_TOKENS = {"TRUST"}
_SMALL_WORDS = {"of", "and", "the", "at", "for", "on", "in", "to", "by", "a"}


def entity_label(name: str, max_words: int = 5) -> str:
    """A company name a human would actually type into a text.

    Only the LEGAL suffix is stripped. PROPERTIES, HOLDINGS, GROUP and PARTNERS
    are part of the brand, and removing them turned "GDP Properties" into a
    bare "GDP".
    """
    words = (name or "").replace(",", " ").split()
    while words and words[-1].upper().strip(".") in {
            s.strip(".") for s in _LEGAL_SUFFIX}:
        words.pop()
    words = words[:max_words]
    while words and words[-1].lower() in _SMALL_WORDS:
        words.pop()
    out = []
    for i, w in enumerate(words):
        if i and len(w) > 1 and w.lower() in _SMALL_WORDS:
            out.append(w.lower())
        elif len(w) <= 3 and w.isupper():
            out.append(w)          # GDP, RHB, TN
        else:
            out.append(w.capitalize())
    return " ".join(out) or (name or "").title()


def buyer_greeting(raw_name: str, first: str = "",
                   is_entity: bool = None) -> str:
    """Who the message is addressed to.

    A known person gets their first name. Everything else is an entity, and the
    split between "Smithbilt team" and "Thresa L Steidlmayer Trust" keys on the
    literal legal token TRUST, NOT on guessing whether a name looks like a
    person. Three keyword classifiers were tried and each produced nonsense:
    "Hi Dr," for D.R. Horton, "Hi Deal," for Deal Finder, "Hi Around," for
    Around The Clock. A legal token is present or it is not.
    """
    if first:
        return first
    # A PERSON whose first name we could not determine gets NO addressee.
    # Falling through to the entity branch produced 'Hi Haddad Amer Michael
    # team,' and 'Hi William David Faulkner Sr team,' on two real rows,
    # which is worse than the bare initial it was meant to fix. Only an
    # entity can be addressed as a team.
    if is_entity is False:
        return ""
    if not (raw_name or "").strip():
        # Neither a person nor an entity name. Returning " team" here rendered
        # a live "Hi  team," and an empty string rendered "Hi ,". Say nothing
        # and let render_deal drop the addressee entirely.
        return ""
    lab = entity_label(raw_name)
    words = [w.upper().strip(".,") for w in (raw_name or "").split()]
    toks = set(words)
    trusty = bool(toks & _TRUST_TOKENS)
    if not trusty and words and words[-1] in ("ESTATE", "ESTATES"):
        trusty = len(words) < 2 or words[-2] != "REAL"
    return lab if trusty else lab + " team"


def render_addr(cohort: str, seed: str, who: str, address: str, price: str,
                beds: str, baths: str, sqft: str, sender: str,
                note: str = "") -> str:
    """Render an ADDRESS-FORWARD touch: the full address is the message.

    Deliberately a different function from render_deal, for the same reason
    render_deal is separate from render(): the merge fields differ, and a
    shared function would let a caller pass a full street address into a
    field that is supposed to hold a road name only. Here the address IS the
    point, so it gets its own entry point and its own audit mode.
    """
    pool = ADDR_POOLS.get(cohort or "cold") or DEAL_ADDR_COLD
    n = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    addressee = (who or "").strip()
    if note and addressee:
        addressee = addressee + " " + note

    def _fill(variant):
        return " ".join(variant.format(
            who=addressee, address=address, price=price, beds=beds,
            baths=baths, sqft=sqft, sender=sender).split())

    order = [pool[n % len(pool)]]
    order += [v for v in pool if v not in order]
    text = _fill(order[0])
    if len(text) > MAX_SMS:
        fits = [t for t in (_fill(v) for v in order) if len(t) <= MAX_SMS]
        if fits:
            text = min(fits, key=len)
    for lead in ("Hi", "Hey", "Hello"):
        text = text.replace(lead + " , ", lead + ", ")
    return text


def render_deal(touch: int, seed: str, who: str, city: str, road: str,
                price: str, beds: str, baths: str, sqft: str,
                sender: str, note: str = "") -> str:
    """Render one buyer touch for a specific deal.

    Separate from `render()` because the merge fields genuinely differ. A seller
    touch fills {addr} and {city}; a deal blast fills {road}, {price} and the
    specs. Sharing one function would let a caller quietly pass a full street
    address where a road name belongs, which is the one thing Ty asked never to
    send.

    `note` is Ty's transparency line for entities, inserted after the greeting
    rather than trailing the question, so it never dangles after a question mark.
    """
    if not 1 <= touch <= len(BUYER_POOLS):
        raise ValueError("buyer touch must be 1-" + str(len(BUYER_POOLS)))
    pool = BUYER_POOLS[touch - 1]
    n = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    addressee = (who or "").strip()
    if note and addressee:
        addressee = addressee + " " + note
    def _fill(variant):
        return " ".join(variant.format(
            who=addressee, city=city or "the area", road=road, price=price,
            beds=beds, baths=baths, sqft=sqft, sender=sender).split())

    order = [pool[(n // (7 ** (touch - 1))) % len(pool)]]
    order += [v for v in pool if v not in order]
    text = _fill(order[0])
    if len(text) > MAX_SMS:
        # A long company name plus the transparency note pushed the chosen
        # variant to 326 characters and the validator rejected it, silently
        # costing a real buyer. Trimming the NAME was worse: it produced
        # 'Affordable Houses and Real team'. So keep the name intact and
        # fall back to the shortest variant that fits.
        fits = [t for t in (_fill(v) for v in order) if len(t) <= MAX_SMS]
        if fits:
            text = min(fits, key=len)
    # Every variant opens "Hi {who}," so an unknown addressee would ship a live
    # "Hi , I saw...". Collapse it to a greeting that reads like a real text.
    for lead in ("Hi", "Hey", "Hello"):
        text = text.replace(lead + " , ", lead + ", ")
    return text


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


def render(touch: int, seed: str, first: str, addr: str, city: str, sender: str,
           variant: str = "owner") -> str:
    """Render one touch (1-4). `seed` makes selection deterministic per record.

    `variant` picks the wording: "owner" asks whether the property is theirs,
    "heir" asks who looks after it. Passing the owner copy to a relative is the
    bug this parameter exists to prevent.
    """
    if not 1 <= touch <= 4:
        raise ValueError("touch must be 1-4")
    if variant not in ("owner", "heir"):
        raise ValueError("variant must be 'owner' or 'heir'")
    pool, noname = (HEIR_POOLS if variant == "heir" else POOLS)[touch - 1]
    chosen = noname if not first else pool
    n = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    template = chosen[(n // (7 ** (touch - 1))) % len(chosen)]
    text = template.format(first=first, addr=addr, city=city or "the area", sender=sender)
    return re.sub(r"\s+", " ", text).strip()
