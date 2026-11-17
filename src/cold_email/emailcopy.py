"""The cold email copy: three versions of one email, in Ty's voice, with a checker.

House rules (from the B2C Cold Email Lab course, in our words, plus SiftStack's own):
  * Start a conversation, never pitch. Short, plain, personal, one easy question, an opt-out by reply.
  * Honest subject lines. No links, images, tracking, unsubscribe links, caps or emoji, and none of the
    words that read as an ad ("cash", "guaranteed", "act now", "free").
  * Spintext {{RANDOM|a|b|c}} on every line and every subject; never on the sender name or the postal
    address. Three versions per step, each with its own subject (one step for a list of 5,000 or more).
  * Never name the list or the reason a house is on it (foreclosure, probate, taxes, a death...): the
    owner should feel found, not targeted. No em or en dash anywhere.
  * Ty's voice: "Hey", short paragraphs, a bare "Thanks" to close.
Placeholders: {{firstName}} (set to "there" when no usable first name), {{street}}, {{city}} and
{{accountSignature}} (the inbox signature, Ty's name only). A postal address line is added from .env
(COLD_EMAIL_ADDRESS) by footer() only when one is set; Ty took his own address out on 2026-11-16, and
CAN-SPAM wants a postal address in a commercial email, so a PO box or a virtual mailbox goes there.
"""
from __future__ import annotations

import itertools
import random
import re

GREET = "Hey {{firstName}}{{RANDOM|,|!}}"
CLOSE = ["{{RANDOM|Thanks|Thanks!|Appreciate it}}", "{{accountSignature}}"]

# Each version: a subject and paragraphs; each paragraph is sentences joined by one space.
VERSIONS = [
    {
        "subject": "{{RANDOM|Quick question about|Question about|About your place on}} {{street}}",
        "paras": [
            ["{{RANDOM|My name is Ty and I buy houses around|I'm Ty, I buy a few houses a year around|This is Ty, I'm a local buyer around}} {{city}}.",
             "{{RANDOM|I came across your place on|I was looking at your property on|I noticed your house on}} {{street}} "
             "{{RANDOM|and wanted to see if you'd ever think about selling it.|and figured I'd ask you directly.|and wanted to ask you about it.}}"],
            ["{{RANDOM|I buy as is, so there's nothing to fix up or clean out.|I'd buy it as it sits, no repairs or cleanup on your end.|No repairs and no showings, I'd take it as is.}}",
             "{{RANDOM|Would you be open to an offer?|Would you be open to hearing a number?|Is selling it something you'd be open to?}}"],
            ["{{RANDOM|If not, just let me know and I won't email you again.|If you'd rather not hear from me, just say so and I won't email again.|If it's a no, just reply and tell me and I'll leave you alone.}}"],
        ],
    },
    {
        "subject": "{{RANDOM|Any plans for|Thoughts on selling|Selling}} {{street}}?",
        "paras": [
            ["{{RANDOM|I had a question about|Wanted to ask you about|Quick one about}} {{street}} in {{city}}.",
             "{{RANDOM|I'm Ty, a local buyer, and I pick up a few houses a year around here.|My name is Ty and I buy houses locally.|I'm Ty, I buy houses in the area and yours caught my eye.}}"],
            ["{{RANDOM|I'd buy it as is and close whenever works for you.|I buy them as they are and work around your timing.|There'd be no fixing up or showings on your end, and you'd pick the closing date.}}",
             "{{RANDOM|Have you thought about selling it at all?|Any chance you'd consider selling it?|Is selling it something you'd think about?}}"],
            ["{{RANDOM|And if you're not interested, just tell me and I won't email again.|If not, no worries, just let me know and I'll stop emailing.|If the answer is no, just reply no and I won't bother you again.}}"],
        ],
    },
    {
        "subject": "{{RANDOM|Interested in|Your house on|Looking at}} {{street}}",
        "paras": [
            ["{{RANDOM|I'm Ty, a local investor here in|My name is Ty and I'm a local buyer in|This is Ty, I buy houses in}} {{city}}.",
             "{{RANDOM|I'd like to make you an offer on|I'm interested in buying|I wanted to see about buying}} {{street}} "
             "{{RANDOM|as is.|in whatever shape it's in.|without you having to fix anything.}}"],
            ["{{RANDOM|Would you consider selling if the number made sense?|Would you be open to selling if the price was right?|If the price worked, would you think about selling?}}"],
            ["{{RANDOM|If not, just say so and I won't email you again.|If you're not interested, just let me know and I'll take you off my list.|If it's a no, just reply and tell me and I won't follow up.}}"],
        ],
    },
]

RANDOM_RX = re.compile(r"\{\{RANDOM\|([^{}]*)\}\}")
VAR_RX = re.compile(r"\{\{(firstName|street|city|accountSignature)\}\}")
BANNED = ("cash", "guarantee", "act now", "free", "urgent", "limited time", "foreclos", "probate", "auction",
          "tax", "lien", "inherit", "estate", "passed away", "death", "deceased", "behind on", "eviction",
          "code violation", "divorce", "bankrupt", "vacant", "http", "www.", ".com", "click")


def footer(address_env: str) -> str:
    """'3153 Example Dr|Town, ST 00000' -> one plain line under the signature."""
    return ", ".join(p.strip() for p in (address_env or "").split("|") if p.strip())


def text_of(version: dict, address: str) -> str:
    """The template as plain text with blank lines between paragraphs."""
    blocks = [GREET] + [" ".join(p) for p in version["paras"]] + ["\n".join(CLOSE + ([address] if address else []))]
    return "\n\n".join(blocks)


def html_of(version: dict, address: str) -> str:
    """What Instantly is sent: one <div> per line, the shape its own editor stores. Bare text
    between <br> tags is stripped on save (seen 2026-11-16: the stored body was ten <br /> and no
    words), so a body is only trusted after a read-back finds the words."""
    from html import escape
    lines = text_of(version, address).split("\n")
    return "".join(f"<div>{escape(l, quote=False)}</div>" if l else "<div><br /></div>" for l in lines)


def render(template: str, lead: dict, rng: random.Random, signature: str = "Your Name") -> str:
    out = RANDOM_RX.sub(lambda m: rng.choice(m.group(1).split("|")), template)
    vals = {"firstName": lead.get("first_name") or "there", "street": lead.get("street", ""),
            "city": lead.get("city", ""), "accountSignature": signature}
    return VAR_RX.sub(lambda m: vals[m.group(1)], out)


def _combos(template: str):
    groups = [g.split("|") for g in RANDOM_RX.findall(template)]
    parts = RANDOM_RX.split(template)            # literal, group, literal, group, ...
    for pick in itertools.product(*groups):
        s = parts[0]
        for i, choice in enumerate(pick):
            s += choice + parts[2 * i + 2]
        yield s


def audit(address: str) -> list[str]:
    """Every rendered combination of every version, checked. Empty list means it passes."""
    problems = []
    sample = {"first_name": "Pat", "street": "100 Example St", "city": "Knoxville"}
    for n, v in enumerate(VERSIONS, 1):
        for s in _combos(v["subject"]):
            if len(s) > 60 or any(c in s for c in "\u2014\u2013"):
                problems.append(f"version {n} subject: {s!r}")
        body_t = text_of(v, "")
        count = 0
        for body in _combos(body_t):
            count += 1
            low = body.lower()
            words = len(body.split())
            if "\u2014" in body or "\u2013" in body or "..." in body:
                problems.append(f"version {n}: a dash or ellipsis")
            if body.count("?") != 1:
                problems.append(f"version {n}: {body.count('?')} questions in one render")
            hit = [b for b in BANNED if b in low]
            if hit:
                problems.append(f"version {n}: banned {hit}")
            if words > 95:
                problems.append(f"version {n}: {words} words")
            if re.search(r"\b[A-Z]{3,}\b", body.replace("RANDOM", "")):
                problems.append(f"version {n}: caps")
            if len(problems) > 20:
                return problems
        problems += [] if count else [f"version {n}: no combinations"]
    if any(c in (address or "") for c in "\u2014\u2013"):
        problems.append("postal address has a dash")
    for n, v in enumerate(VERSIONS, 1):
        if "{{RANDOM|" not in v["subject"]:
            problems.append(f"version {n}: subject without spintext")
        for line in text_of(v, address).splitlines():
            if line.strip() in ("", address, "{{accountSignature}}"):
                continue                     # sender name and postal address stay plain
            if "{{RANDOM|" not in line:
                problems.append(f"version {n}: a line without spintext: {line!r}")
    return sorted(set(problems))


def combos_count() -> int:
    total = 0
    for v in VERSIONS:
        total += sum(1 for _ in _combos(v["subject"])) * sum(1 for _ in _combos(text_of(v, "")))
    return total


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
    from setup_accounts import env
    addr = footer(env("COLD_EMAIL_ADDRESS"))
    probs = audit(addr)
    print("versions:", len(VERSIONS), "| distinct emails:", f"{combos_count():,}")
    print("audit:", "PASS" if not probs else "FAIL")
    for p in probs[:20]:
        print("  ", p)
    rng = random.Random(3)
    lead = {"first_name": "Pat", "street": "100 Example St", "city": "Knoxville"}
    for v in VERSIONS:
        print("\n---\nSubject:", render(v["subject"], lead, rng))
        print(render(text_of(v, addr), lead, rng))
