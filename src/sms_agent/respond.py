"""Reply generation.

The playbook is the stable system prompt (cached); the record facts and the
thread go in the user turn, which is the volatile half. That ordering is what
keeps the Anthropic prompt cache warm across a day of replies.

Whatever the model returns is then run through `validate()`, which is a hard
gate rather than a suggestion: a message that names a dollar amount, carries a
link, or runs long is blocked even if the model was confident. Prompting sets
the intent; the validator is what actually holds the line.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from . import config, knowledge

log = logging.getLogger(__name__)

MAX_SMS_CHARS = 320

# Anything that reads as a dollar figure. Deliberately broad: "around 90k",
# "$85,000", "90 thousand", "mid 100s" all get caught.
_MONEY = re.compile(
    r"(\$\s*\d)"
    r"|(\b\d[\d,]*\s*(k|K)\b)"
    r"|(\b\d[\d,]*\s*(thousand|grand|million)\b)"
    r"|(\b(low|mid|high)\s+\d{2,3}s?\b)"
    r"|(\b\d{2,3}\s*[-to]{1,3}\s*\d{2,3}\s*(k|K)\b)",
)
_LINK = re.compile(r"(https?://)|(www\.)|(\b[\w-]+\.(com|net|org|io|co|us|info|link)\b)", re.I)
# Machine-written tells. Kept in sync with the AI_TELLS list in the
# text-touch-builder skill (references/message-recipe.md, "Sound human, or do
# not send it"), because outbound touches and inbound replies land in the same
# thread on the same phone and must sound like the same person.
_BANNED = re.compile(
    r"\b(opportunity|solution|reach out|circle back|touch base|no obligation|"
    r"absolutely|certainly|leverage|utilize|elevate|seamless|unleash|delve|"
    r"streamline|robust|empower|tailored|curated|comprehensive|myriad|holistic|"
    r"synerg\w+|additionally|furthermore|moreover|nevertheless|"
    r"feel free to|do ?n[o']?t hesitate|at your earliest convenience)\b",
    re.I,
)
_FORM_LETTER = re.compile(r"\bI hope this (message|email) finds you well\b", re.I)
# Nobody uses a semicolon in a text message.
_SEMICOLON = re.compile(r";")
_SHOUTING = re.compile(r"!{2,}|\b[A-Z]{4,}\b")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")

# "Never name the list" is the rule the whole program rests on: the seller
# should feel found, not targeted. Naming how we found them is what turns a
# friendly text into a complaint, so it is enforced here and not left to the
# prompt. The agent may acknowledge these if the OWNER raises them; that path
# routes to a human, so a draft containing one of these words is always wrong.
_LIST_WORDS = re.compile(
    r"\b(foreclos\w*|auction\w*|probate|decedent|inherit\w*|estate sale|"
    r"tax (lien|sale|delinq\w*)|delinquen\w*|lien|code violation|condemn\w*|"
    r"evict\w*|divorce|bankrupt\w*|behind on (your )?(payment|mortgage|taxes)|"
    r"pre-?foreclosure|trustee sale|distress\w*|default)\b",
    re.I,
)

# A zip code in an SMS means the agent pasted a database row.
_FULL_ADDRESS = re.compile(r"\b\d{5}(-\d{4})?\b")


@dataclass
class Reply:
    message: str = ""
    confidence: float = 0.0
    handoff: bool = False
    reason: str = ""
    ok: bool = False
    blocked: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "confidence": round(self.confidence, 3),
            "handoff": self.handoff,
            "reason": self.reason,
            "ok": self.ok,
            "blocked": self.blocked,
        }


SCHEMA = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "The SMS to send. Empty string if nothing should be sent.",
        },
        "confidence": {
            "type": "number",
            "description": "Honest probability a senior acquisitions manager would send this as-is.",
        },
        "handoff": {
            "type": "boolean",
            "description": "True if a human should take over the conversation now.",
        },
        "reason": {"type": "string", "description": "One short sentence of rationale."},
    },
    "required": ["message", "confidence", "handoff", "reason"],
    "additionalProperties": False,
}

_TASK = """You write the next single SMS in this thread, following the playbook above exactly.

Return JSON with:
- message: the text to send, or "" if the right move is to send nothing
- confidence: your honest probability that a senior acquisitions manager would send this exact message without editing it. Be strict. If you are unsure about a fact, the situation, or the tone, that is a low number.
- handoff: true if a human should take the conversation from here
- reason: one short sentence

When handoff is true, `message` should be a short holding reply that does not promise a time or a number, or "" if the last thing to do is say nothing at all.
Use ONLY the facts listed under FACTS. If a fact is not listed, you do not know it."""


def _locality(context: dict) -> str:
    """How the agent describes where it operates, since it may never name a company.

    County reads more local and less corporate than a city, which is why the
    proven outbound copy leans on it.
    """
    county = (context.get("county") or "").strip()
    if county:
        return county if county.lower().endswith("county") else f"{county} County"
    city = (context.get("city") or "").strip()
    if city:
        return city
    return config.LOCALITY_FALLBACK


def _identity_block(context: dict) -> str:
    """Who the thread is from.

    The name is the person ACTUALLY ASSIGNED to the record, so the name in the
    text is the name that calls. Left to itself the model invents one (it
    introduced itself as "Alex" the first time this was tested), and a
    fabricated name is a lie the seller discovers the moment a real person
    calls. So an unresolved assignee means no name, never a plausible one.

    A company name is never given: a named company is litigation bait and gives
    a hostile recipient something concrete to file against. The agent describes
    itself by locality instead.
    """
    name = (context.get("assigned_name") or config.SENDER_NAME or "").strip()
    lines = ["IDENTITY:"]
    if name:
        lines.append(
            f"- Your first name in this thread is exactly: {name}."
            " This is the person assigned to this record and the person who will call."
            " Use it when you introduce yourself and when you sign off."
        )
    else:
        lines.append(
            "- You have NO name to give. Never invent one, never sign a name, and never"
            " answer a 'what is your name' question with a name. Say someone from the"
            " team will introduce themselves when they call."
        )
    where = _locality(context)
    if where:
        lines.append(
            f"- Describe yourself by locality only: a local buyer here in {where}."
            " NEVER say a company name, ours or any other."
        )
    else:
        lines.append(
            "- Say you are 'a local buyer'. NEVER say a company name, ours or any other,"
            " and never invent a locality you were not given."
        )
    return "\n".join(lines)


def _facts_block(context: dict) -> str:
    """The only facts the responder may use.

    Deliberately thin. Valuation, equity, tax delinquency, foreclosure dates,
    liens, vacancy and every list tag are withheld entirely: the responder
    cannot leak what it was never given, and "the seller should feel found, not
    targeted" is the rule the whole program rests on.

    Street line only, never the full address with state and zip. Beds, baths
    and square footage are withheld too, because reciting them reads like a
    database record, which is exactly what it is.
    """
    if not context:
        return "FACTS: none available. You know only what is in the thread."
    order = [
        ("owner_first", "Owner first name (empty means use owner-of-the-address wording)"),
        ("street", "Property street line (use EXACTLY this, never add city, state or zip)"),
        ("city", "City"),
        ("county", "County"),
    ]
    lines = [f"- {label}: {context[key]}" for key, label in order if context.get(key)]
    if not lines:
        return "FACTS: none available. You know only what is in the thread."
    return "FACTS (the only property facts you may use):\n" + "\n".join(lines)


def _deal_facts_block(context: dict) -> str:
    """The only deal facts the dispo responder may use.

    Mirrors `_facts_block`'s discipline with the opposite content. The road name
    and the price are here because the message exists to carry them. The house
    number and zip are NOT here at all: they are not withheld by a rule the
    model could talk itself out of, they are simply absent from what it is
    given, which is the only reliable way to not send something.

    ARV, rehab and rent ARE included, because the agent needs to know what is
    true to recognise a question it must hand off. The playbook forbids stating
    them, and the validator's price whitelist blocks them if it tries.

    On an ADDRESS-FORWARD deal the full address IS included, because the
    message exists to carry it. The LOCKBOX CODE is never included on any
    deal: that code is physical access to the house, so it is a human send
    on request. The agent may offer it; it cannot give it.
    """
    if not context:
        return "FACTS: none available. You know only what is in the thread."
    order = [
        ("buyer_first", "Buyer first name (empty means greet nobody by name)"),
        # Present ONLY on address-forward deals. On a redacted deal this key
        # is absent from the context entirely, so the model has nothing to
        # leak rather than a rule it could talk itself out of.
        ("address", "Full property address (you MAY state this one)"),
        ("road", "Road name ONLY (never add a house number, city or zip)"),
        ("area", "Area or part of town"),
        ("county", "County"),
        ("price", "Asking price (the ONLY figure you may state)"),
        ("beds", "Bedrooms"),
        ("baths", "Bathrooms"),
        ("sqft", "Square feet"),
        ("year", "Year built"),
        ("rehab_level", "Rough rehab level"),
        ("status", "Deal status"),
    ]
    lines = [f"- {label}: {context[key]}" for key, label in order if context.get(key)]
    hidden = [k for k in ("arv_note", "rehab_cost", "rent") if context.get(k)]
    if hidden:
        lines.append("- Context you may NOT state, for recognising a handoff: "
                     + ", ".join(f"{k}={context[k]}" for k in hidden))
    if not lines:
        return "FACTS: none available. You know only what is in the thread."
    return "FACTS (the only deal facts you may use):\n" + "\n".join(lines)


def _thread_block(thread: list[dict]) -> str:
    if not thread:
        return "THREAD: (no prior messages)"
    out = ["THREAD (oldest first):"]
    for m in thread[-20:]:
        who = "OWNER" if m.get("direction") == "in" else "US"
        out.append(f"{who}: {m.get('body', '')}")
    return "\n".join(out)


# Individual money tokens, so the buyer profile can check WHICH number was
# said rather than only whether one was. Same coverage as _MONEY above.
_MONEY_TOKEN = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d+)?\s*[kK]?"
    r"|\b\d[\d,]*\s*[kK]\b"
    r"|\b\d[\d,]*\s*(?:thousand|grand|million)\b"
    r"|\b(?:low|mid|high)\s+\d{2,3}s?\b",
)


def _norm_money(token: str) -> Optional[int]:
    """A money token as a plain integer, or None if it is deliberately vague."""
    t = token.strip().lower()
    if re.match(r"^(low|mid|high)\b", t):
        return None  # "mid 100s" is not a price you can hold someone to
    mult = 1
    if re.search(r"\bmillion\b", t):
        mult = 1_000_000
    elif re.search(r"\b(thousand|grand)\b", t) or re.search(r"\d\s*k$", t):
        mult = 1_000
    digits = re.sub(r"[^\d.]", "", re.sub(r"(thousand|grand|million|k)", "", t))
    if not digits:
        return None
    try:
        return int(round(float(digits) * mult))
    except ValueError:
        return None


def validate(message: str, max_questions: int = 1, program: str = "seller",
             allowed_prices: Optional[list] = None,
             allowed_address: str = "") -> tuple[bool, list[str]]:
    """Hard gate. Returns (ok, reasons blocked).

    `max_questions` is 1 for AI-written replies, where two questions in a row
    reads as a bot interrogating someone. The proven OUTBOUND touches pass 2,
    because a confirm-then-rephrase ("does 12 Elm St happen to be yours? Do I
    have the right person?") is how people actually text and it is already
    tested copy. Blocking it silently held 18% of the cohort.

    `program` selects the profile. "seller" is the acquisition agent and is
    unchanged. "buyer" is the dispo agent, where three rules invert because the
    job inverts:

      * A price is the POINT of a deal blast, so money is allowed, but only a
        figure from the approved deal sheet (`allowed_prices`). A number the
        model made up is still blocked, which is the whole reason this is a
        whitelist and not simply a relaxed rule.
      * Distress vocabulary ("foreclosure", "estate") is ordinary deal
        description when you are selling TO an investor rather than texting a
        homeowner, so the name-the-list rule does not apply.
      * Zip codes stay blocked, but the check runs on the text with approved
        prices removed. Otherwise a legitimate $92,000 reads as a 5-digit zip.
    """
    if program not in ("seller", "buyer"):
        raise ValueError("program must be 'seller' or 'buyer'")
    problems = []
    text = (message or "").strip()
    if not text:
        return False, ["empty"]
    if len(text) > MAX_SMS_CHARS:
        problems.append(f"too long ({len(text)} > {MAX_SMS_CHARS})")

    zip_text = text
    if program == "seller":
        if _MONEY.search(text):
            problems.append("contains a dollar amount or price signal")
    else:
        allowed = {int(p) for p in (allowed_prices or [])}
        seen = _MONEY_TOKEN.findall(text)
        for tok in seen:
            val = _norm_money(tok)
            if val is None:
                problems.append(f"vague price wording ('{tok.strip()}')")
            elif val not in allowed:
                problems.append(
                    f"price '{tok.strip()}' is not on the approved deal sheet")
        # Approved prices are not zip codes.
        zip_text = _MONEY_TOKEN.sub(" ", text)

    if _LINK.search(text):
        problems.append("contains a link")
    if program == "seller":
        hit = _LIST_WORDS.search(text)
        if hit:
            problems.append(f"names the list ('{hit.group(0)}'); the seller must feel found, not targeted")
    if allowed_address:
        # ADDRESS-FORWARD deals (Ty, blast 2): the full address IS the
        # message, so its zip must not read as a leaked database row. Only
        # THIS address is exempt, matched literally, so a different address
        # or a bare stray zip is still blocked. Same shape as the price
        # whitelist: permit exactly the approved value, nothing near it.
        zip_text = zip_text.replace(allowed_address, " ")
    if _FULL_ADDRESS.search(zip_text):
        problems.append("contains a zip code; use the street line only")
    hit = _BANNED.search(text)
    if hit:
        problems.append(f"machine-written wording ('{hit.group(0)}')")
    if _FORM_LETTER.search(text):
        problems.append("form-letter opener")
    if _SEMICOLON.search(text):
        problems.append("semicolon; nobody uses one in a text message")
    if _SHOUTING.search(text):
        problems.append("stacked exclamation marks or shouting in caps")
    if _EMOJI.search(text):
        problems.append("emoji")
    if "—" in text or "–" in text:
        problems.append("contains an em/en dash")
    if text.count("?") > max_questions:
        problems.append(f"asks more than {max_questions} question(s)")
    if re.search(r"\b(i am|i'm)\s+(an?\s+)?(ai|bot|automated)", text, re.I):
        problems.append("self-identifies as automated; that is a human handoff, not a reply")
    return (not problems), problems


def draft(
    thread: list[dict],
    context: Optional[dict] = None,
    intent: str = "",
    intent_rationale: str = "",
    program: str = "seller",
) -> Reply:
    """Draft the next message. Never sends; the caller decides what to do with it.

    `program` picks the system prompt, the facts the model is shown, and the
    validator profile applied to its output. All three have to move together:
    handing the dispo prompt a seller facts block would leave it with no price
    to quote, and validating a dispo draft with the seller profile would block
    the very price it was told to send.
    """
    if not config.ANTHROPIC_API_KEY:
        return Reply(reason="no ANTHROPIC_API_KEY", blocked=["no model access"])
    try:
        import anthropic
    except ImportError:
        return Reply(reason="anthropic SDK not installed", blocked=["no model access"])

    prompt_program = "dispo" if program == "buyer" else "seller"
    system = [
        {
            "type": "text",
            "text": knowledge.playbook(prompt_program),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    facts = _deal_facts_block if program == "buyer" else _facts_block
    user = (
        f"{_identity_block(context or {})}\n\n"
        f"{facts(context or {})}\n\n"
        f"{_thread_block(thread)}\n\n"
        f"Classifier read the latest owner message as: {intent or 'unknown'}"
        f"{' (' + intent_rationale + ')' if intent_rationale else ''}\n\n"
        f"{_TASK}"
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=system,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001
        return Reply(reason=f"model error: {exc}"[:200], blocked=["model error"])

    if getattr(resp, "stop_reason", "") == "refusal":
        return Reply(reason="model refused", handoff=True, blocked=["refusal"])

    body = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return Reply(reason="unparseable model output", blocked=["parse error"])

    reply = Reply(
        message=str(data.get("message", "")).strip(),
        confidence=max(0.0, min(1.0, float(data.get("confidence", 0.0)))),
        handoff=bool(data.get("handoff")),
        reason=str(data.get("reason", ""))[:300],
    )
    if not reply.message:
        reply.ok = False
        reply.blocked = ["model chose to send nothing"]
        return reply

    allowed = []
    if program == "buyer" and (context or {}).get("price_value"):
        allowed = [(context or {})["price_value"]]
    # The address is whitelisted the same way the price is: a dispo
    # agent that is told the address and then blocked from saying it
    # would fail the one question every buyer asks next.
    allowed_addr = ""
    if program == "buyer":
        allowed_addr = (context or {}).get("address") or ""
    ok, problems = validate(reply.message, program=program,
                            allowed_prices=allowed,
                            allowed_address=allowed_addr)
    reply.ok = ok
    reply.blocked = problems
    if not ok:
        log.warning("reply blocked (%s): %s", ", ".join(problems), reply.message[:120])
    return reply
