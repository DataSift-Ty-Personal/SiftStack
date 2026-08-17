#!/usr/bin/env python3
"""Build four personalized text touches per record from a DataSift CSV export.

Self-contained (Python 3.8+, standard library only). Reads the CSV you exported
from your filter preset, generates Text Touch 1-4 for every record following the
message recipe (see references/message-recipe.md), and writes an import-ready
CSV you upload back into DataSift with the four columns mapped to your
Text Touch custom fields.

Variant selection is seeded by the record's address + owner, so:
- neighboring records get DIFFERENT message sequences (cold-email style rotation)
- re-running on a grown export regenerates IDENTICAL text for existing records

Usage:
  python build_text_touches.py exported_records.csv
  python build_text_touches.py export.csv --out touches.csv --sender Maria
  python build_text_touches.py export.csv --col-street "Street" --col-first "First Name"

Sign-off resolution: the "Assigned To" column when present (first name only),
else --sender. Records with neither are skipped and reported.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

MAX_CHARS = 320  # 2 SMS segments; pools aim well under 160

ENTITY_RX = re.compile(
    r"\b(llc|l\.l\.c|inc|corp|trust|trustee|estate|properties|property|holdings|"
    r"investments|ventures|partners|lp|llp|company|bank|assoc|church|city of|county)\b",
    re.I,
)

# {first} owner first name, {addr} street line, {city} city, {sender} caller name.
# Rewrite these in YOUR voice; keep the structure (see references/message-recipe.md).
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
POOLS = [(TOUCH1, TOUCH1_NONAME), (TOUCH2, TOUCH2_NONAME),
         (TOUCH3, TOUCH3_NONAME), (TOUCH4, TOUCH4_NONAME)]

# common DataSift export header spellings, first match wins (case-insensitive)
COLUMN_GUESSES = {
    "street": ["property street", "street address", "property address", "street", "address"],
    "city": ["property city", "city"],
    "state": ["property state", "state"],
    "zip": ["property zip", "property zip code", "zip", "zip code", "postal code"],
    "first": ["owner first name", "first name", "owner 1 first name"],
    "last": ["owner last name", "last name", "owner 1 last name"],
    "owner": ["owner name", "owner full name", "owner"],
    "assigned": ["assigned to", "assignee", "assigned"],
}


def detect(headers: list[str], key: str, override: str) -> str:
    if override:
        for h in headers:
            if h.strip().lower() == override.strip().lower():
                return h
        sys.exit(f"FATAL: column {override!r} not found in the CSV. Headers: {headers}")
    lower = {h.strip().lower(): h for h in headers}
    for guess in COLUMN_GUESSES[key]:
        if guess in lower:
            return lower[guess]
    return ""


def _is_name_token(tok: str) -> bool:
    return len(tok) >= 2 and tok.replace("'", "").replace("-", "").isalpha()


def clean_first(raw: str) -> str:
    """Usable first name, or '' when there is none.

    'C Eugene Suthard' -> 'Eugene'. 'E A Henry' -> '' (no-name variants).

    The second case is the subtle one and was wrong until 2026-08: taking "the
    first token of length 2 or more" walks past the initials and lands on the
    SURNAME, so an initials-only owner got greeted by their last name ("Hi
    Henry!"). The fix is positional. On a multi-token name only the tokens
    BEFORE the surname can supply a first name; if they are all initials, we do
    not have one.
    """
    tokens = (raw or "").replace(".", " ").split()
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].title() if _is_name_token(tokens[0]) else ""
    for tok in tokens[:-1]:  # everything except the surname
        if _is_name_token(tok):
            return tok.title()
    return ""


# ---------------------------------------------------------------------------
# Human-voice check. Runs on every generated message AND on the variant pools
# themselves, so a tell that gets pasted into a pool is caught before it ever
# reaches a seller rather than after.
#
# The em dash is the single clearest signal that text was machine-written. Real
# people texting from a phone do not produce one. Same for the en dash, the
# semicolon (nobody uses a semicolon in a text message), and the corporate
# vocabulary below.
AI_TELLS = [
    (re.compile(r"[—–]"), "em or en dash (the clearest machine-written tell)"),
    (re.compile(r";"), "semicolon (nobody uses one in a text message)"),
    (re.compile(r"\bI hope this (message|email) finds you well\b", re.I), "form-letter opener"),
    (re.compile(r"\b(I wanted to |just wanted to )?reach out\b", re.I), "reach out"),
    (re.compile(r"\b(circle back|touch base|at your earliest convenience)\b", re.I), "corporate filler"),
    (re.compile(r"\b(please )?do(n't| not) hesitate\b", re.I), "do not hesitate"),
    (re.compile(r"\bfeel free to\b", re.I), "feel free to"),
    (re.compile(r"^\s*(Absolutely|Certainly|Indeed|Great question)\b", re.I), "assistant-style opener"),
    (re.compile(r"\b(Additionally|Furthermore|Moreover|Nevertheless)\b", re.I), "essay connective"),
    (re.compile(r"\b(delve|navigate|landscape|streamline|robust|leverage|utilize|"
                r"seamless|elevate|unlock|empower|tailored|curated|comprehensive|"
                r"myriad|holistic|synerg\w+)\b", re.I), "AI vocabulary"),
    (re.compile(r"\b(no obligation|zero obligation|100% free|act now|limited time)\b", re.I),
     "pitch language"),
    (re.compile(r"(https?://|www\.)", re.I), "link (gets the number filtered)"),
    (re.compile(r"[\U0001F300-\U0001FAFF☀-➿]"), "emoji"),
    (re.compile(r"!{2,}"), "stacked exclamation marks"),
    (re.compile(r"\b[A-Z]{4,}\b"), "shouting in all caps"),
]


def human_voice_problems(text: str) -> list[str]:
    """Every reason this message does not read like a person wrote it."""
    return [why for rx, why in AI_TELLS if rx.search(text)]


def audit_pools() -> list[str]:
    """Check the variant pools before generating anything.

    A bad variant is worse than a bad record: it goes to everyone who hashes
    onto it.
    """
    problems = []
    for name, pool in (("TOUCH1", TOUCH1), ("TOUCH1_NONAME", TOUCH1_NONAME),
                       ("TOUCH2", TOUCH2), ("TOUCH2_NONAME", TOUCH2_NONAME),
                       ("TOUCH3", TOUCH3), ("TOUCH3_NONAME", TOUCH3_NONAME),
                       ("TOUCH4", TOUCH4), ("TOUCH4_NONAME", TOUCH4_NONAME)):
        for i, variant in enumerate(pool):
            for why in human_voice_problems(variant):
                problems.append(f"{name}[{i}]: {why} -> {variant[:70]}")
    return problems


def render(seed_text: str, first: str, addr: str, city: str, sender: str,
           no_name: bool) -> list[str]:
    s = int(hashlib.md5(seed_text.encode()).hexdigest(), 16)
    out = []
    for i, (pool, noname_pool) in enumerate(POOLS):
        p = noname_pool if (no_name or not first) else pool
        msg = p[(s // (7 ** i)) % len(p)].format(
            first=first, addr=addr, city=city, sender=sender)
        out.append(re.sub(r"\s+", " ", msg).strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("csv_in", nargs="?", help="CSV exported from your DataSift filter preset")
    ap.add_argument("--check-pools", action="store_true",
                    help="audit the variant pools for AI tells and exit")
    ap.add_argument("--out", default="text_touches_import.csv")
    ap.add_argument("--sender", default="", help="fallback signer when no Assigned To column/value")
    ap.add_argument("--show", type=int, default=3, help="sample records to print")
    for k in ("street", "city", "first", "last", "owner", "assigned"):
        ap.add_argument(f"--col-{k}", default="", help=f"exact header of the {k} column")
    a = ap.parse_args()

    # Audit the variant pools first. A tell in a pool reaches everyone who
    # hashes onto it, so this runs on every invocation, not just on request.
    pool_problems = audit_pools()
    if pool_problems:
        print("POOL PROBLEMS (fix these before sending anything):")
        for problem in pool_problems:
            print("  " + problem)
        if not a.check_pools:
            sys.exit("FATAL: variant pools contain machine-written tells.")
    elif a.check_pools:
        print("All variant pools read human. No em dashes, no AI tells.")
    if a.check_pools:
        return 0
    if not a.csv_in:
        sys.exit("FATAL: a CSV is required (or pass --check-pools).")

    rows = list(csv.DictReader(open(a.csv_in, encoding="utf-8-sig")))
    if not rows:
        sys.exit("FATAL: no rows in the input CSV")
    headers = list(rows[0].keys())

    col = {k: detect(headers, k, getattr(a, f"col_{k}", ""))
           for k in COLUMN_GUESSES}
    if not col["street"]:
        sys.exit(f"FATAL: could not find a street address column. Headers: {headers}\n"
                 "Pass it explicitly with --col-street \"Your Header\"")
    print("column mapping:", {k: v for k, v in col.items() if v})

    out_rows, skipped = [], []
    for r in rows:
        street = (r.get(col["street"]) or "").strip()
        if not street:
            skipped.append(("(blank street)", "no street address"))
            continue
        city = (r.get(col["city"]) or "").strip() if col["city"] else ""
        raw_first = (r.get(col["first"]) or "") if col["first"] else ""
        if not raw_first and col["owner"]:
            raw_first = (r.get(col["owner"]) or "").split(" ")[0]
        owner_full = " ".join(x.strip() for x in [
            r.get(col["first"], "") if col["first"] else "",
            r.get(col["last"], "") if col["last"] else ""] if x.strip()) \
            or (r.get(col["owner"], "") if col["owner"] else "")
        no_name = bool(ENTITY_RX.search(owner_full or raw_first))
        first = "" if no_name else clean_first(raw_first)

        sender = ""
        if col["assigned"]:
            sender = clean_first(r.get(col["assigned"]) or "")
        if not sender:
            sender = a.sender.strip().title()
        if not sender:
            skipped.append((street, "no Assigned To value and no --sender"))
            continue

        touches = render(f"{street}|{owner_full}".lower(), first, street,
                         city or "the area", sender, no_name)
        if any(len(t) > MAX_CHARS for t in touches):
            skipped.append((street, f"a touch exceeded {MAX_CHARS} chars"))
            continue
        # A message that reads as machine-written is worse than no message.
        tells = [w for t in touches for w in human_voice_problems(t)]
        if tells:
            skipped.append((street, "does not read human: " + "; ".join(sorted(set(tells)))))
            continue
        out = {col["street"]: street}
        for k in ("city", "state", "zip"):
            if col[k]:
                out[col[k]] = (r.get(col[k]) or "").strip()
        for i, t in enumerate(touches, 1):
            out[f"Text Touch {i}"] = t
        out["_sender"] = sender
        out_rows.append(out)

    if not out_rows:
        sys.exit("FATAL: nothing renderable. Check the column mapping.")

    fields = [c for c in (col["street"], col["city"], col["state"], col["zip"]) if c]
    fields += [f"Text Touch {i}" for i in range(1, 5)]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    senders: dict[str, int] = {}
    for r in out_rows:
        senders[r["_sender"]] = senders.get(r["_sender"], 0) + 1
    print(f"\nrendered {len(out_rows)} records -> {a.out}")
    print(f"skipped {len(skipped)}", f"e.g. {skipped[:5]}" if skipped else "")
    print(f"signer distribution: {senders}")
    print("\nsamples:")
    for r in out_rows[: a.show]:
        print(f"\n  {r[col['street']]}  (signer: {r['_sender']})")
        for i in range(1, 5):
            t = r[f"Text Touch {i}"]
            print(f"    T{i} ({len(t)}c): {t}")
    print("\nNext: upload this CSV via Add Data into the SAME list (upserts by address),"
          "\nand drag Text Touch 1-4 onto your custom fields in the mapping step.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
