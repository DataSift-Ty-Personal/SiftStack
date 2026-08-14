"""Reply classification.

Deterministic rules run FIRST and are final for the two buckets that carry
legal or data consequences. An LLM must never be the thing that decides
whether somebody opted out.

Ported from the proven `_api/mms_responses.py` classifier with one change that
matters for a conversational agent: OPT_OUT and NOT_INTERESTED are separate.
The batch classifier lumped "not interested" into opt-out, which is correct
when you only ever suppress, and wrong here: a soft no is a conversation a
closer handles, a legal opt-out is one you must honor and never text again.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from . import config

# Legal opt-out. Carrier keywords plus the natural-language forms a human would
# obviously understand. In a back-and-forth, ignoring "stop texting me" because
# it was not the literal word STOP is indefensible.
OPT_OUT = [
    r"^\s*(stop|quit|end|cancel|unsubscribe|revoke|optout)\s*[.!]*\s*$",
    r"\bstop\s+(texting|messaging|contacting|calling|sending)\b",
    r"\bunsubscribe\b",
    r"\bopt\s*-?\s*out\b",
    r"\b(remove|take)\s+(me|this\s+number)\s+(off|from)\b",
    r"\bremove\s+(this\s+)?(number|me)\b",
    r"\bdo\s*n[o']?t\s+(contact|text|message|call)\s+me\b",
    r"\bdon'?t\s+(contact|text|message|call)\s+me\b",
    r"\bno\s+more\s+(texts|messages|calls)\b",
    r"\bleave\s+me\s+alone\b",
    r"\blose\s+my\s+number\b",
    r"\bnever\s+(text|contact|message)\s+me\b",
]

# Strong signals only. A wrong-number flip writes to the CRM and permanently
# suppresses a number, so it must not fire on "I don't think so".
WRONG_NUMBER = [
    r"\bwrong\s+(number|person|guy|gal|address)\b",
    r"\byou(?:'ve| have)?\s+(?:got|have)\s+the\s+wrong\b",
    r"\bi\s+(?:do\s*n[o']?t|don'?t)\s+own\b",
    r"\bnever\s+owned\b",
    r"\bdon'?t\s+own\s+(?:that|any|a\b|this)\b",
    r"\bnot\s+my\s+(house|home|property|address)\b",
    r"\bthat'?s\s+not\s+my\b",
    r"\bnot\s+mine\b",
    r"\bno\s+(?:such\s+)?property\b",
    r"\bi\s+don'?t\s+have\s+(?:a|any)\s+(?:house|home|property)\b",
    r"\bi'?m\s+not\b.*\b(the\s+owner|that\s+person)\b",
    r"\bwho(?:'s| is)\s+that\b.*\bnot\s+me\b",
    # The text opens "Hi <owner name>", so the tell that we reached the wrong
    # person is them asking about that name in the third person, or denying it
    # outright. Both landed unclassified on live threads (2026-08-11):
    # "Who the hell is Jonathan" and "this ain't joseph."
    # The lookahead keeps "who is this" as ASKING_WHO: asking who WE are is a
    # normal question from the right person, and must not disposition a good
    # number as wrong.
    r"\bwho(?:'s|\s+is|\s+the\s+(?:hell|heck|f\w*)\s+is)\s+(?!this\b|that\b|it\b|there\b)[a-z]{3,}\b",
    r"\b(?:this|that)\s+(?:ain'?t|is\s*n[o']?t|isn'?t)\s+(?!my\b|the\b|a\b)[a-z]{3,}\b",
    r"\bno\s*(?:one|body)\s+here\s+by\s+that\s+name\b",
    r"\bthere'?s\s+no\s+\w+\s+here\b",
]

# Soft no. Handled by a closer, not by suppression.
NOT_INTERESTED = [
    r"\bnot\s+interested\b",
    r"\bno\s+thanks?\b",
    r"\bnot\s+(selling|for\s+sale)\b",
    r"\bi'?m\s+good\b",
    r"\bnot\s+at\s+this\s+time\b",
    r"\bnot\s+right\s+now\b",
    r"\balready\s+(sold|listed|under\s+contract)\b",
    r"\bhave\s+an?\s+(agent|realtor)\b",
]

# Retention language: they are telling you they intend to KEEP the house.
#
# This exists because of a real miss (Jessica, 6956 Cardindale, 2026-08-11).
# Touch 1 asks an ownership question, so a reply often answers two questions at
# once: "It is. And it's staying that way." The first half confirms the number,
# the second half is the actual answer, and the classifier read only the first
# and paged the prospector on a homeowner who had just declined.
#
# Ty's read: mark the phone CORRECT and close it as not interested. Someone
# saying "it's staying that way" is expressing attachment to the house, and
# calling them as a hot lead is the fastest way to burn a number that a
# patient follow-up could still convert later.
KEEPING_IT = [
    r"\b(stay|stays|staying)\s+that\s+way\b",
    r"\bnot\s+going\s+anywhere\b",
    r"\b(plan|planning|plans|intend|want)\s+(on\s+|to\s+)?keep(ing)?\b",
    r"\b(keeping|gonna\s+keep|going\s+to\s+keep|will\s+keep)\s+(it|the\s+(house|property|home))\b",
    r"\bstay(ing)?\s+in\s+the\s+family\b",
    r"\b(never|not|won'?t|will\s+not)\s+(be\s+)?(going\s+to\s+|gonna\s+)?sell(ing)?\b",
    r"\bit'?s\s+not\s+for\s+sale\b",
]

# Confirming the number is right is NOT a signal about selling. Touch 1 asks
# "is <street> yours?", so these answer THAT question and nothing more.
OWNER_CONFIRMED = [
    r"^\s*(yes|yeah|yep|yup|correct|it\s+is|that'?s\s+(right|correct|me|mine)|sure\s+is)\b",
    r"^\s*(i|we)\s+(do|am|own\s+it)\b",
    r"\b(that|it)\s+is\s+(my|our)\s+(house|property|home|address)\b",
]

INTERESTED = [
    r"\binterested\b",
    r"\bhow\s+much\b",
    r"\bwhat'?s\s+(the|your)\s+offer\b",
    r"\bmake\s+an?\s+offer\b",
    r"\bcall\s+me\b",
    r"\btell\s+me\s+more\b",
    r"\bi\s+need\s+help\b",
    r"\bhow\s+can\s+you\s+help\b",
    r"\bwhat\s+can\s+you\s+do\b",
    r"\bwhat\s+do\s+you\s+(offer|do)\b",
    r"\bwhat\s+are\s+you\s+(offering|thinking)\b",
    r"\bcash\s+offer\b",
    r"\bdepends\s+on\s+the\s+(price|number|offer)\b",
]

ASKING_WHO = [
    r"\bwho\s+(is|'?s|are\s+you|dis|this)\b",
    r"\bwhat\s+is\s+this\b",
    r"\bwhat'?s\s+this\b",
    r"\bwhat\s+are\s+you\s+talking\b",
    r"\bhow\s+did\s+you\s+get\b",
    r"\bwhere\s+did\s+you\s+get\b",
    r"\bwhat\s+(property|address|house)\b",
    r"\bnew\s+phone\b",
    r"\bis\s+this\s+a\s+(bot|scam|robot)\b",
]

# Anything here forces a human regardless of what else matched. These are the
# situations where a wrong word from an automated system does real damage.
ESCALATE_NOW = [
    r"\b(lawyer|attorney|sue|suing|legal\s+action|cease\s+and\s+desist)\b",
    r"\b(passed\s+away|died|deceased|funeral|in\s+hospice)\b",
    r"\b(bankrupt|bankruptcy|chapter\s+(7|13))\b",
    r"\bauction\s+is\s+(today|tomorrow)\b",
    r"\b(harass|harassment|report\s+you|fcc|attorney\s+general)\b",
]

INTENTS = (
    "OPT_OUT",
    "WRONG_NUMBER",
    "INTERESTED",
    "NOT_INTERESTED",
    "ASKING_WHO",
    "ESCALATE",
    "OTHER",
    "EMPTY",
)


@dataclass
class Classification:
    intent: str
    confidence: float
    source: str  # rules | llm | fallback
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "rationale": self.rationale,
        }


def _hit(text: str, patterns: list[str]) -> Optional[str]:
    for p in patterns:
        if re.search(p, text):
            return p
    return None


def classify_rules(text: str) -> Optional[Classification]:
    """Deterministic pass. Returns None when nothing fires with confidence."""
    t = (text or "").strip().lower()
    if not t:
        return Classification("EMPTY", 1.0, "rules")

    hit = _hit(t, ESCALATE_NOW)
    if hit:
        return Classification("ESCALATE", 1.0, "rules", f"sensitive: {hit}")
    hit = _hit(t, OPT_OUT)
    if hit:
        return Classification("OPT_OUT", 1.0, "rules", f"opt-out: {hit}")
    hit = _hit(t, WRONG_NUMBER)
    if hit:
        return Classification("WRONG_NUMBER", 0.95, "rules", f"wrong-number: {hit}")

    # Decided here rather than by the model, because the model already got this
    # wrong once: an ownership confirmation sitting in front of a refusal read
    # as enthusiasm. Whoever confirms and then says the house is staying put has
    # answered, and the answer is no.
    hit = _hit(t, KEEPING_IT)
    if hit:
        return Classification(
            "NOT_INTERESTED", 0.9, "rules",
            f"soft no, intends to keep the property: {hit}",
        )
    return None


SYSTEM = """You classify one inbound SMS from a property owner replying to a real estate acquisition text.

Return exactly one intent:
- INTERESTED: engages about selling, asks price/offer/process, asks to be called, says yes, or gives any opening.
- NOT_INTERESTED: a soft no. Not selling, already listed, has an agent, "no thanks". This is NOT a legal opt-out.
- WRONG_NUMBER: says they are not the owner, do not own the property, or the number belongs to someone else.
- ASKING_WHO: does not know who is texting or how you got their info, asks what property, or asks if this is a bot or a scam.
- ESCALATE: mentions a lawyer, a death, bankruptcy, harassment, a regulator, or anything a human must handle personally.
- OTHER: anything that fits none of the above.

Rules:
- Judge only this message in the context of the thread. Do not infer motivation that is not there.
- The first text asks whether an address belongs to them. So "yes", "it is", "correct", "that's mine" answer the OWNERSHIP question. Confirming ownership is not a signal about selling, and on its own it is OTHER, never INTERESTED.
- When a reply confirms ownership AND says anything about keeping the house ("it is, and it's staying that way", "yes, but I'm not selling", "that's mine and I plan to keep it"), the second half is the answer. Label it NOT_INTERESTED. The confirmation only tells you the phone number is right.
- INTERESTED needs a signal about SELLING: a price question, an opening, a request to be called, a reason they might move. Attachment to the house is the opposite of that signal.
- If the message could plausibly be two intents, pick the more conservative one (ESCALATE over INTERESTED, ASKING_WHO over INTERESTED, NOT_INTERESTED over INTERESTED) and lower your confidence.
- confidence is your honest probability that a careful human would assign the same label. Do not inflate it.
- Never label a message OPT_OUT. Opt-outs are decided before you see the message."""

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["INTERESTED", "NOT_INTERESTED", "WRONG_NUMBER", "ASKING_WHO", "ESCALATE", "OTHER"],
        },
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["intent", "confidence", "rationale"],
    "additionalProperties": False,
}


def classify_llm(text: str, history: Optional[list[dict]] = None) -> Classification:
    if not config.ANTHROPIC_API_KEY:
        return Classification("OTHER", 0.0, "fallback", "no ANTHROPIC_API_KEY")
    try:
        import anthropic
    except ImportError:
        return Classification("OTHER", 0.0, "fallback", "anthropic SDK not installed")

    convo = ""
    for m in (history or [])[-8:]:
        who = "OWNER" if m.get("direction") == "in" else "US"
        convo += f"{who}: {m.get('body', '')}\n"

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS,
            system=SYSTEM,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": f"Thread so far:\n{convo or '(none)'}\n\nClassify this new inbound message:\n{text}",
                }
            ],
        )
    except Exception as exc:  # network, rate limit, refusal-adjacent
        return Classification("OTHER", 0.0, "fallback", f"llm error: {exc}"[:200])

    if getattr(resp, "stop_reason", "") == "refusal":
        return Classification("OTHER", 0.0, "fallback", "model refused")

    body = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return Classification("OTHER", 0.0, "fallback", "unparseable model output")

    conf = float(data.get("confidence", 0.0))
    return Classification(
        data.get("intent", "OTHER"),
        max(0.0, min(1.0, conf)),
        "llm",
        str(data.get("rationale", ""))[:300],
    )


def classify_rules_wrong_number(text: str) -> bool:
    """Does this message also carry a wrong-number signal?

    Used when an opt-out and a wrong number arrive in the same sentence, so the
    phone gets marked WRONG for the callers as well as suppressed for texting.
    """
    return bool(_hit((text or "").strip().lower(), WRONG_NUMBER))


def classify(text: str, history: Optional[list[dict]] = None) -> Classification:
    """Rules first, model second. The rules pass is authoritative when it fires."""
    ruled = classify_rules(text)
    if ruled is not None:
        return ruled

    llm = classify_llm(text, history)
    if llm.source == "llm" and llm.confidence >= 0.5:
        return llm

    # Model unavailable or unsure. Fall back to the weak keyword buckets, which
    # only ever route to a human anyway.
    t = (text or "").strip().lower()
    for patterns, intent in (
        (NOT_INTERESTED, "NOT_INTERESTED"),
        (INTERESTED, "INTERESTED"),
        (ASKING_WHO, "ASKING_WHO"),
    ):
        hit = _hit(t, patterns)
        if hit:
            return Classification(intent, 0.6, "rules", f"keyword: {hit}")
    return Classification("OTHER", 0.3, llm.source, llm.rationale or "no signal")
