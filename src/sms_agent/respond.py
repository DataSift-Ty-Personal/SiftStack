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


def _thread_block(thread: list[dict]) -> str:
    if not thread:
        return "THREAD: (no prior messages)"
    out = ["THREAD (oldest first):"]
    for m in thread[-20:]:
        who = "OWNER" if m.get("direction") == "in" else "US"
        out.append(f"{who}: {m.get('body', '')}")
    return "\n".join(out)


def validate(message: str, max_questions: int = 1) -> tuple[bool, list[str]]:
    """Hard gate. Returns (ok, reasons blocked).

    `max_questions` is 1 for AI-written replies, where two questions in a row
    reads as a bot interrogating someone. The proven OUTBOUND touches pass 2,
    because a confirm-then-rephrase ("does 12 Elm St happen to be yours? Do I
    have the right person?") is how people actually text and it is already
    tested copy. Blocking it silently held 18% of the cohort.
    """
    problems = []
    text = (message or "").strip()
    if not text:
        return False, ["empty"]
    if len(text) > MAX_SMS_CHARS:
        problems.append(f"too long ({len(text)} > {MAX_SMS_CHARS})")
    if _MONEY.search(text):
        problems.append("contains a dollar amount or price signal")
    if _LINK.search(text):
        problems.append("contains a link")
    hit = _LIST_WORDS.search(text)
    if hit:
        problems.append(f"names the list ('{hit.group(0)}'); the seller must feel found, not targeted")
    if _FULL_ADDRESS.search(text):
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
) -> Reply:
    """Draft the next message. Never sends; the caller decides what to do with it."""
    if not config.ANTHROPIC_API_KEY:
        return Reply(reason="no ANTHROPIC_API_KEY", blocked=["no model access"])
    try:
        import anthropic
    except ImportError:
        return Reply(reason="anthropic SDK not installed", blocked=["no model access"])

    system = [
        {
            "type": "text",
            "text": knowledge.playbook(),
            "cache_control": {"type": "ephemeral"},
        }
    ]
    user = (
        f"{_identity_block(context or {})}\n\n"
        f"{_facts_block(context or {})}\n\n"
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

    ok, problems = validate(reply.message)
    reply.ok = ok
    reply.blocked = problems
    if not ok:
        log.warning("reply blocked (%s): %s", ", ".join(problems), reply.message[:120])
    return reply
