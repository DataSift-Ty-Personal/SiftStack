"""The heir lane (build plan 3.5): a researched record worked heir by heir.

Research ends with who signs and in what order. Before this lane that answer sat in a note. The
record stayed in whatever cold preset it came from, a caller dialed the dead owner's numbers and
asked for the dead owner by name (on the first five records researched, 2026-09-23, three owners
were dead and all three were still being called and texted by name), and the heirs' own numbers
and addresses were used by nobody.

The rules:

  * WHO IS IN IT. A record that carries the heir tag (the research write-back adds it) and whose
    research names at least one VERIFIED signer: a person in `must_sign` whose status is not
    labelled UNVERIFIED and whom the analysis shows living. A signer line that names a role or a
    group ("the heirs of ...") names nobody. A tagged record with no research result on this box is
    listed for a person and left alone: the lane never guesses who signs.
  * ONE DEDICATED CALLER. Researched records go to one person (decision D19).
  * ONE HEIR AT A TIME, in the order the research set (`call_order`). The next heir's turn starts
    when the current heir has had a full round of call days with no conversation, or every number
    of theirs is marked wrong or dead. A conversation holds the turn: the caller has it.
  * TEXTED BY NAME, ABOUT THE PROPERTY, NEVER THE DEATH. We are often wrong about who died, and a
    text that presumes a death reaches a person who has lost nobody, or tells somebody something
    they did not know. The copy lives here with its own audit, and a variant that names a death
    is refused before anything is staged.
  * THREE TO SIX HANDWRITTEN LETTERS PER VERIFIED SIGNER, AT THEIR OWN ADDRESS. The address comes
    from the research (where that person lives), never from the record: the record's mailing
    address is the dead owner's. A signer with no address of their own gets no letter.
  * THE ROSTER STOPS THE MOMENT THE RECORD LEAVES THE LANE. A record that became a lead, lost the
    tag or no longer passes the gate has its staged pieces pulled on the next run, and every piece
    is checked again when the batch is released, so nobody gets two mail series at once.
  * NOTHING IS SENT FROM HERE. Letters and texts are staged for a person to release. The caller's
    assignment and the one call task per turn are written directly: they tell a teammate what to
    do and reach no homeowner.

The gate decides who may be touched. A stage of this kind is given the allow words `heir` (a dead
owner's record may be worked once research tagged a verified heir) and `in_research` (the record
may still read Deep Prospecting). Nothing here lifts anything else, and nothing here marks an owner
deceased, swaps an owner or changes a status.

BLOUNT COUNTY has no obituary index, so more Blount signers come back from research as UNVERIFIED.
The lane treats them like any other unverified signer: counted, never called as a signer.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import activity, gate, lanes, ledger, limits, mail, researchqueue, researchroutes
from .stages import Ctx, Summary, addr, assignee_of, resolve_callers

log = logging.getLogger(__name__)

KIND_MAIL, KIND_TEXT = "heir_mail", "heir_text"
TURN_FACT = "heir_turn"           # whose turn it is on a record, and since when
TASK_FACT = "heir_task"           # the call task written for the current turn (live runs only)
MAX_SMS = 320

DEFAULTS = {
    "research_workflow": "deep_prospect",
    "heir_caller": None,            # the dedicated caller: a first name or {name, uuid} (decision D19)
    "attempts_per_heir": 5,         # call days before the next heir's turn (Ready to Call plus attempts 1 to 4)
    "conversation_seconds": 60,
    "letters_per_heir": 4,          # three to six
    "letter_every_days": 30,
    "letter_piece": "handwritten_letter",
    "letter_copy": None,            # optional list of letter bodies, one per letter; the pool below otherwise
    "texts": True,
    "text_system": "smarter_contact",   # the texting tool the file is loaded into (an owner keeps one thread)
    "texts_per_heir": 1,
    "text_gap_days": 3,
    "daily_pieces": 30,
    "daily_texts": 30,
    "max_records": 100,
    "max_scan": 2000,
    "scope_tags": None,             # default: the gate's heir tags
    "mailed_tag": "Mailed {yyyy}-{mm}",
    "last_mailed_field": "Last Mailed",
}

# ---------------------------------------------------------------------- the copy, and its audit
# A death is never named, in any of the ways a person might. "Estate" is refused whole, which also
# keeps "real estate" out: that phrase reads as a pitch and the word reads as a death to an heir.
DEATH_WORDS = re.compile(
    r"\b(d(?:ie|ied|ies|ying)|dead|death|deceased|decedent|pass(?:ed|ing)|loss|lost|estate|obituar(?:y|ies)|"
    r"probate|inherit\w*|heirs?|funeral|condolences?|sympath(?:y|ies)|bereave\w*|grie(?:f|ve|ves|ving)|mourn\w*|"
    r"widow\w*|memorial|executor|executrix|"
    r"late (?:husband|wife|mother|father|mom|dad|parents?|brother|sister|son|daughter))\b", re.I)
DASHES = (chr(0x2014), chr(0x2013))

# Touch 1 asks about the property by the heir's name. Touch 2 is the same question once more.
TEXTS = [
    [
        "Hi {first}, this is {sender}. I wanted to ask you about the house at {street}. Do you have a few minutes to talk this week?",
        "Hi {first}! My name is {sender}. I'm reaching out about the property at {street}. Would you be the right person to talk with about it?",
        "Hey {first}, {sender} here. I'd love to ask you a couple of quick questions about {street}. Is there a good time to call?",
        "Hi {first}, I'm {sender}, a local buyer. I'm interested in {street} and hoped to talk with you about it. Would a short call work?",
        "Hi {first}, this is {sender}. I have a question about the place on {street}. Are you the person to ask?",
    ],
    [
        "Hi {first}, {sender} again. I wasn't sure my text about {street} came through. Is there a good time to talk about it?",
        "Hey {first}, just trying once more about {street}. Would a quick call this week work for you? {sender}",
        "Hi {first}, {sender} here. Still hoping to talk with you about {street}. Is this a good number for you?",
    ],
]
# One body per letter, in order. A handwritten letter needs a body the mail house can copy out.
LETTERS = [
    "Hi {first}, my name is {sender}. I am writing about the house at {street}. If you would ever consider an offer on it, I would be glad to talk. You can call or text me at {phone}.",
    "Hi {first}, {sender} here again. I wrote a few weeks ago about {street}. I buy houses in the area as they sit, with nothing to fix or clean out. If that would help, my number is {phone}.",
    "Hi {first}, I wanted to follow up on my note about {street}. If the timing is not right, no problem at all. If it ever is, I can make it simple. {sender}, {phone}.",
    "Hi {first}, this is {sender}. I am still interested in {street} and would be glad to answer any questions. One call is enough to see if it makes sense. {phone}.",
    "Hi {first}, a quick note from {sender} about {street}. I can buy it as it is and work around your schedule. If you would like a number on it, call or text {phone}.",
    "Hi {first}, this is my last note about {street}. If you ever want to talk about it, I would be glad to hear from you. {sender}, {phone}.",
]


def audit_copy(text: str, *, first: str = "", street: str = "", sms: bool = True) -> list[str]:
    """Everything wrong with one piece of heir copy, in words. Empty means it may go out.

    The death words are the rule this lane exists for. The rest are the texting agent's own rules
    (never name the list, one question, no link, no zip, no dash), kept here so a letter or a text
    to an heir cannot be looser than a text to an owner."""
    out = []
    t = str(text or "")
    m = DEATH_WORDS.search(t)
    if m:
        out.append("names a death (%r)" % m.group(0).lower())
    m = mail.LIST_WORDS.search(t)
    if m:
        out.append("names the list (%r)" % m.group(0).lower())
    if any(d in t for d in DASHES):
        out.append("carries a long dash")
    if re.search(r"https?://|www\.|\.com\b", t, re.I):
        out.append("carries a link")
    if "$" in t:
        out.append("names a dollar amount")
    if ";" in t:
        out.append("carries a semicolon")
    if "!!" in t:
        out.append("stacks exclamation marks")
    if any(ord(ch) > 126 for ch in t):
        out.append("carries a character a phone may not show")
    bare = t.replace(street, " ") if street else t
    if re.search(r"\b\d{5}\b", bare):
        out.append("carries a zip code")
    if re.search(r"\b[A-Z]{4,}\b", bare):
        out.append("shouts a word in capitals")
    if "{" in t or "}" in t:
        out.append("has an unfilled blank")
    if first and first.lower() not in t.lower():
        out.append("does not use the person's name")
    if street and street.lower() not in t.lower():
        out.append("does not name the property")
    if sms:
        if len(t) > MAX_SMS:
            out.append("is over %d characters" % MAX_SMS)
        if t.count("?") > 1:
            out.append("asks more than one question")
    return out


def render_text(touch: int, seed: str, first: str, street: str, sender: str) -> str:
    """One heir text. The variant is picked by the record and the heir, so a re-run writes the same
    words and two heirs of one house do not get the same sentence."""
    pool = TEXTS[max(1, min(touch, len(TEXTS))) - 1]
    n = int(hashlib.md5(("%s|%d" % (seed, touch)).encode()).hexdigest(), 16)
    return " ".join(pool[n % len(pool)].format(first=first, street=street, sender=sender).split())


def render_letter(number: int, first: str, street: str, sender: str, phone: str, bodies: list | None = None) -> str:
    pool = [str(b) for b in (bodies or []) if str(b).strip()] or LETTERS
    body = pool[(max(1, number) - 1) % len(pool)]
    return " ".join(body.format(first=first or "there", street=street, sender=sender, phone=phone).split())


def audit_pools(bodies: list | None = None) -> list[str]:
    """Every text and letter variant, filled with sample values and audited. A stage refuses to
    stage anything while this is not empty."""
    out = []
    for ti, pool in enumerate(TEXTS, 1):
        for vi, tpl in enumerate(pool, 1):
            text = " ".join(tpl.format(first="Kim", street="12 Oak St", sender="Dana").split())
            for p in audit_copy(text, first="Kim", street="12 Oak St"):
                out.append("text %d variant %d %s" % (ti, vi, p))
    pool = [str(b) for b in (bodies or []) if str(b).strip()] or LETTERS
    for li, tpl in enumerate(pool, 1):
        try:
            text = " ".join(tpl.format(first="Kim", street="12 Oak St", sender="Dana", phone="865-555-0100").split())
        except (KeyError, IndexError, ValueError) as exc:
            out.append("letter %d has a blank the lane cannot fill (%s)" % (li, exc))
            continue
        for p in audit_copy(text, first="", street="12 Oak St", sms=False):
            out.append("letter %d %s" % (li, p))
    return out


# ---------------------------------------------------------------------- who signs, from the research
STRUCTURAL = re.compile(r"\b(estate|heirs?|executor|executrix|administrat(?:or|rix)|representative|trustees?|trust|"
                        r"successor|children|spouse|unknown|whoever|next of kin|family)\b", re.I)
GENERATION = {"jr", "sr", "ii", "iii", "iv"}


def _tokens(name: str) -> tuple[list[str], str]:
    """(the name's real words, its generational suffix). Initials and suffixes are not words."""
    toks = re.sub(r"[^a-z' -]", " ", str(name or "").lower()).replace("-", " ").split()
    gen = next((t for t in toks if t in GENERATION), "")
    return [t for t in toks if len(t) > 1 and t not in GENERATION], gen


def same_person(a: str, b: str) -> bool:
    """Do two spellings name one person? The FIRST name must agree, because a son often carries his
    father's first name as his middle name, and Sr. and Jr. are two people. A lone first name ("Kim"
    in a call order) matches the full name it starts."""
    ta, ga = _tokens(a)
    tb, gb = _tokens(b)
    if not ta or not tb or ta[0] != tb[0]:
        return False
    if ga and gb and ga != gb:
        return False
    if len(ta) == 1 or len(tb) == 1:
        return True
    return ta[-1] == tb[-1]


def heir_key(name: str) -> str:
    toks, gen = _tokens(name)
    return " ".join(toks + ([gen] if gen else []))


@dataclass
class Heir:
    key: str
    name: str
    first: str = ""                # the name a text or a letter may greet, or ""
    relationship: str = ""
    numbers: list = field(default_factory=list)      # in the research's order
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    def mailable(self) -> bool:
        return bool(self.street and self.city and self.state and len(self.zip) == 5)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_research(record_uuid: str, workflow: str = "deep_prospect") -> dict | None:
    """What research found on a record: the result the deep prospect wrote (agent_result.json in
    its run folder, the same result the worker keeps on the task) plus, when the run folder is on
    this box, the analysis's people and the addresses the trace returned for them. None when this
    box holds no finished research on the record."""
    res = researchqueue.last_result(record_uuid, workflow)
    if not res:
        return None
    run_dir = Path(str(res.get("run_dir") or "")) if res.get("run_dir") else None
    if run_dir is not None and run_dir.is_dir():
        on_file = _read_json(run_dir / "agent_result.json")
        if isinstance(on_file, dict) and on_file.get("must_sign") is not None:
            res = dict(on_file, _task=res.get("_task"), _finished=res.get("_finished"))
    out = {"task": res.get("_task"), "finished": res.get("_finished"), "owner_status": str(res.get("owner_status") or ""),
           "decision_maker": str(res.get("decision_maker") or ""), "must_sign": res.get("must_sign") or [],
           "call_order": res.get("call_order") or [], "slug": str(res.get("slug") or ""), "people": [], "index": {},
           # The write-back's own list of verified people (an opened record names them AND a number
           # matches their name). None on a result written before that list existed.
           "verified": [str(n) for n in res["verified"]] if isinstance(res.get("verified"), list) else None,
           "level": int(res.get("research_level") or 0)}
    if run_dir is not None and run_dir.is_dir() and out["slug"]:
        an = _read_json(run_dir / "analysis" / (out["slug"] + ".json"))
        if isinstance(an, dict):
            out["people"] = [p for p in (an.get("people") or []) if isinstance(p, dict)]
        groups = _read_json(run_dir / "people.json")
        for g in groups if isinstance(groups, list) else []:
            if isinstance(g, dict) and g.get("slug") == out["slug"]:
                out["index"] = {p.get("key"): p for p in (g.get("people") or []) if isinstance(p, dict)}
    return out


def _own_address(person: dict | None, index: dict) -> dict:
    """Where this person lives, as the research has it: the address the analysis set for them, else
    the one their own trace returned. Never anything off the record."""
    a = (person or {}).get("trace_address") or {}
    if not a.get("street"):
        sp = index.get((person or {}).get("ss_key") or "") or {}
        a = {"street": sp.get("street"), "city": sp.get("city"), "state": sp.get("state"), "zip": sp.get("zip")}
    return {"street": " ".join(str(a.get("street") or "").split()), "city": " ".join(str(a.get("city") or "").split()),
            "state": str(a.get("state") or "").strip().upper(), "zip": str(a.get("zip") or "").strip()[:5]}


def heirs_of(research: dict) -> tuple[list[Heir], Counter]:
    """(the verified signers in the research's call order, what was left out and why)."""
    notes: Counter = Counter()
    people = research.get("people") or []
    index = research.get("index") or {}
    verified = research.get("verified")
    out: list[Heir] = []
    for m in research.get("must_sign") or []:
        if not isinstance(m, dict):
            continue
        status = str(m.get("status") or "").strip()
        for name in [x.strip() for x in re.split(r",|;|&|\band\b", str(m.get("who") or "")) if x.strip()]:
            if STRUCTURAL.search(name):
                notes["a signer line names a role or a group, not a person"] += 1
                continue
            if len(_tokens(name)[0]) < 2:
                # "J Sample" cannot be matched to a number or greeted by name without guessing.
                notes["a signer named by an initial or one word only: left for a person"] += 1
                continue
            if status.upper().startswith("UNVERIFIED"):
                notes["signer not verified as living: confirm before any call"] += 1
                continue
            person = next((p for p in people if same_person(str(p.get("name") or ""), name)), None)
            if person is not None and str(person.get("living") or "") != "Living":
                notes["signer the research does not show living"] += 1
                continue
            # When the research result carries its own list of verified people, that list decides:
            # a signer named only in an obituary nobody opened, or with no number in their name, is
            # a lead for research, not a person to call as a signer.
            if verified is not None and not any(same_person(v, name) for v in verified):
                notes["signer the research has not verified (no opened record, or no number in their name)"] += 1
                continue
            key = heir_key(name)
            if any(h.key == key for h in out):
                continue
            rel = str((person or {}).get("relationship") or "").strip()
            shown = " ".join(name.split())
            if shown.isupper() or shown.islower():
                shown = shown.title()             # a county-roll spelling; a name research wrote is kept as written
            out.append(Heir(key=key, name=shown, first=mail._clean_first(shown), relationship=rel,
                            **_own_address(person, index)))
    order: list[str] = []
    for c in research.get("call_order") or []:
        if not isinstance(c, dict):
            continue
        n = gate.digits(c.get("number"))
        who = str(c.get("who") or "")
        match = [h for h in out if same_person(h.name, who)]
        if len(match) > 1:
            exact = [h for h in match if heir_key(h.name) == heir_key(who)]
            match = exact if len(exact) == 1 else []
            if not match:
                notes["a number the research lists under a name two signers share: left out"] += 1
        if len(match) != 1 or not n:
            continue
        h = match[0]
        if n not in h.numbers:
            h.numbers.append(n)
        if h.key not in order:
            order.append(h.key)
    first_seen = {h.key: i for i, h in enumerate(out)}
    out.sort(key=lambda h: (order.index(h.key) if h.key in order else len(order) + first_seen[h.key]))
    return out, notes


# ---------------------------------------------------------------------- whose turn it is
def turn_of(ctx: Ctx, rec: dict, heirs: list[Heir], usable: set, o: dict) -> dict:
    """Whose turn it is to be called on this record.

    Returns {"heir", "position", "of", "state", "days"}: state is `calling` (work this heir),
    `talked` (a conversation happened: the caller has it) or `done` (every heir with a number has
    had a full round). The turn is worked out from the account's own call log, so it moves whichever
    dialer made the calls."""
    u = rec.get("uuid") or ""
    owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
    phones = {gate.digits(p.get("number")): str(p.get("status") or "UNKNOWN").upper()
              for p in ((rec.get("owner") or {}).get("phones") or [])}
    line = [h for h in heirs if h.numbers]
    if not line:
        return {"heir": None, "position": 0, "of": 0, "state": "done", "days": 0}
    fact = ledger.fact(u, TURN_FACT) or {}
    keys = [h.key for h in line]
    i = keys.index(fact["heir"]) if fact.get("heir") in keys else 0
    since = None
    if fact.get("heir") in keys and fact.get("since"):
        try:
            since = datetime.fromisoformat(fact["since"])
        except ValueError:
            since = None
    need = int(o["attempts_per_heir"])
    talk = int(o["conversation_seconds"])
    while i < len(line):
        h = line[i]
        if since is None:
            since = ctx.now
            ledger.set_fact(u, TURN_FACT, {"heir": h.key, "since": since.isoformat(timespec="seconds")})
        mine = set(h.numbers)
        days, talked = set(), False
        if owner_uuid:
            for e in activity.events((activity.DIAL, "owner.call.answered"), resource=owner_uuid, since=since):
                d = e["data"]
                if d.get("direction") == "inbound" or d.get("number") not in mine:
                    continue
                if e["event_type"] == activity.DIAL:
                    days.add(e["when"].astimezone(ZoneInfo(ctx.policy.tz)).date().isoformat())
                elif int(d.get("duration") or 0) >= talk:
                    talked = True
        if talked:
            return {"heir": h, "position": i + 1, "of": len(line), "state": "talked", "days": len(days)}
        dead_line = all(phones.get(n, "UNKNOWN") in gate.BAD_PHONE for n in h.numbers)
        none_usable = not (mine & usable)
        if len(days) >= need or dead_line or none_usable:
            i += 1
            since = None
            continue
        return {"heir": h, "position": i + 1, "of": len(line), "state": "calling", "days": len(days)}
    return {"heir": None, "position": len(line), "of": len(line), "state": "done", "days": 0}


# ---------------------------------------------------------------------- staged pieces: build, export, pull, release
MAIL_COLS = ["piece", "name", "street", "city", "state", "zip", "first_name", "property_street", "property_city",
             "letter", "of", "text", "record_uuid", "heir"]
TEXT_COLS = ["number", "first_name", "text", "property_street", "record_uuid", "heir"]


def export(batch_id: int, kind: str, items: list[dict]) -> str:
    """The batch as a file a person hands to the mail house or loads into the texting tool."""
    texts = kind.endswith("text")
    out = Path(config.DATA_DIR) / ("texts" if texts else "mail") / ("batch_%d.csv" % batch_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TEXT_COLS if texts else MAIL_COLS, extrasaction="ignore")
        w.writeheader()
        for it in items:
            w.writerow(it)
    return str(out)


def staged_keys(stage_id: str, kinds: tuple) -> set:
    """(record, heir, piece number) for every piece of this stage still waiting on a person."""
    out = set()
    for b in ledger.batches(status="staged", stage=stage_id):
        if b["kind"] not in kinds:
            continue
        for it in (ledger.batch(b["id"]) or {}).get("items") or []:
            out.add((it.get("record_uuid") or "", it.get("heir") or "", b["kind"], int(it.get("letter") or it.get("touch") or 0)))
    return out


def pull_staged(ctx: Ctx, kinds: tuple, gone: dict) -> int:
    """Take the pieces of records that left the lane out of every batch of this stage that is still
    waiting. `gone` is record uuid -> why. Returns how many pieces were pulled. A pulled text stops
    holding its owner's place in the text ledger."""
    pulled = 0
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        if b["kind"] not in kinds:
            continue
        full = ledger.batch(b["id"]) or {}
        keep = [it for it in full.get("items") or [] if it.get("record_uuid") not in gone]
        n = len(full.get("items") or []) - len(keep)
        if not n:
            continue
        pulled += n
        ctx.out.line("batch %d: %d piece(s) pulled before release (the record left the lane)" % (b["id"], n))
        if ctx.live:
            ledger.batch_items_set(b["id"], keep, sum(float(it.get("price") or 0) for it in keep))
            export(b["id"], b["kind"], keep)
            if b["kind"] == KIND_TEXT:
                for u in {it.get("record_uuid") for it in full.get("items") or []} & set(gone):
                    limits.drop_text(b["id"], u)
            if not keep:
                ledger.batch_set(b["id"], "cancelled", {"why": "every piece was pulled before release"})
    return pulled


def expire_texts(ctx: Ctx, today: str) -> None:
    """A text batch is released on its day or not at all, the same rule as the text lane: its send
    times and the owner's limits were worked out for that day. Yesterday's unreleased heir texts are
    closed here, their places in the text ledger given back, and the heirs are staged again."""
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        if b["kind"] != KIND_TEXT:
            continue
        full = ledger.batch(b["id"]) or {}
        day = next((it.get("day") for it in full.get("items") or [] if it.get("day")), "")
        if not day or day >= today:
            continue
        ctx.out.problem("heir text batch %d (%d text(s), staged for %s) was never released, so nothing was sent; "
                        "the heirs are staged again" % (b["id"], len(full.get("items") or []), day))
        if ctx.live:
            limits.drop_text(b["id"], status="expired")
            ledger.batch_set(b["id"], "expired", {"why": "not released on %s" % day})


def text_room(ctx: Ctx, lim, rec: dict, number: str, taken: set, *, exclude_batch: int = 0,
              when: datetime | None = None) -> tuple[bool, str, str]:
    """(room for this heir text, why not, when it may go). The house limits decide, not this lane:
    the sending window on both clocks, and the owner's one text a day and five a week across every
    texting stage and both systems (limits.room). The heirs' numbers sit on the record's owner, so
    that owner is the one counted."""
    tz = ctx.policy.tz
    owner = (rec.get("owner") or {}).get("uuid") or ""
    if owner and owner in taken:
        return False, "the owner already has a text in this run", ""
    now = when or ctx.now
    limits.observe(owner, rec, now, tz, lim)          # a text the other system sent counts too
    _group, at, why = limits.planned_send(number, now.astimezone(ZoneInfo(tz)).date(), lim, tz, not_before=now)
    if at is None:
        return False, why, ""
    r = limits.room(owner, "text", at, policy=ctx.policy, lim=lim, tz=tz, number=number,
                    system=str(ctx.stage.get("text_system", DEFAULTS["text_system"])), campaign=True,
                    exclude_batch=exclude_batch)
    if not r.ok:
        return False, r.why, ""
    return True, "", at.astimezone(timezone.utc).isoformat(timespec="seconds")


def letters_sent(record_uuid: str, stage_id: str, series: str = "heir") -> dict:
    """heir key -> (letters mailed, when the last one went), from the ledger."""
    out: dict[str, tuple[int, str]] = {}
    for e in ledger.events(record_uuid, "mail", stage=stage_id):
        d = e["detail"]
        if d.get("series") != series:
            continue
        k = str(d.get("heir") or "")
        n, _ = out.get(k, (0, ""))
        out[k] = (max(n, int(d.get("step") or 0)), e["at"])
    return out


def still_due(ctx: Ctx, rec: dict, it: dict) -> str:
    """Why a staged heir piece may no longer go out, or ''. Run on every piece at release: the lane
    first (a record that became a lead or lost its tag is out), then the gate."""
    if not rec.get("uuid"):
        return "the record could not be read"
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    lane = lanes.lane_of(rec, lanes_cfg)
    if lane != "heirs":
        return "the record left the heir lane (now %s)" % lane.replace("_", " ")
    text = str(it.get("kind") or "") == "text"
    if text and it.get("day") and it["day"] < ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date().isoformat():
        return "staged for %s and not released that day: the heir is staged again on the next run" % it["day"]
    v = ctx.verdict(rec, "text" if text else "mail")
    if not v.ok:
        return v.reason()
    if text and it.get("number") not in {n["number"] for n in v.numbers}:
        return "the number is no longer usable for a text"
    if text:
        try:
            when = max(datetime.fromisoformat(str(it.get("send_at"))), ctx.now)
        except (TypeError, ValueError):
            when = ctx.now
        ok, why, _ = text_room(ctx, limits.settings(ctx.policy), rec, it["number"], set(),
                               exclude_batch=int(it.get("_batch") or 0), when=when)
        if not ok:
            return why
    return ""


def record_piece(ctx: Ctx, rec: dict, it: dict, ref: str, state: dict) -> None:
    """A released heir piece, written down: on the record (the month tag and the last-mailed date
    an outside mail house's pieces get, and a note), and in the ledger under the heir it went to.
    A letter is booked as mail, so it counts against the outreach budget like any other piece."""
    u = it["record_uuid"]
    today = ledger.day(ctx.now, ctx.policy.tz)
    # The ledger first: it is the roster. A note that fails to land on the record afterwards is a
    # problem to report, not a reason to forget that the piece went out and send it again.
    if str(it.get("kind") or "") == "text":
        ledger.event(ctx.run_id, ctx.stage.id, u, "heir_text", {"heir": it.get("heir"), "touch": int(it.get("touch") or 1),
                                                               "number": it.get("number"), "ref": ref},
                     now=ctx.now, tz=ctx.policy.tz)
        try:
            ctx.crm.message(u, "Texted %s about the property (heir text %d), %s." % (
                it.get("name") or "an heir", int(it.get("touch") or 1), ref), pin=False)
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("the text to an heir of %s is in the ledger, but its note did not land on the record (%s)" % (
                it.get("property_street") or "a record", str(exc)[:80]))
        return
    cost = float(it.get("price") or 0)
    ledger.event(ctx.run_id, ctx.stage.id, u, "mail",
                 {"piece": it.get("piece"), "step": int(it.get("letter") or 1), "vendor": "csv", "ref": ref,
                  "logged": "tag+note", "series": "heir", "heir": it.get("heir"), "heir_name": it.get("name")},
                 usd=round(cost, 4), now=ctx.now, tz=ctx.policy.tz)
    ctx.settle(0, cost, "heir letter")
    try:
        tag = str(ctx.stage.get("mailed_tag", DEFAULTS["mailed_tag"]) or "").format(yyyy=today[:4], mm=today[5:7])
        if tag:
            ctx.crm.add_tags(u, [tag], rec or None)
        if "field" not in state:
            try:
                state["field"] = ctx.crm.custom_field_uuid(str(ctx.stage.get("last_mailed_field", DEFAULTS["last_mailed_field"])))
            except Exception:  # noqa: BLE001
                state["field"] = ""
        if state.get("field"):
            ctx.crm.set_custom_field(u, state["field"], today)
        ctx.crm.message(u, "Mailed: heir letter %d of %d to %s%s at their own address, %s." % (
            int(it.get("letter") or 1), int(it.get("of") or 1), it.get("name") or "a signer",
            (" (%s)" % it["relationship"]) if it.get("relationship") else "", ref), pin=False)
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("the letter to an heir of %s is in the ledger, but the record was not marked (%s)" % (
            it.get("property_street") or "a record", str(exc)[:80]))


def release_roster(batch_id: int, kinds: tuple, recheck, record, *, crm=None, pol=None, commit: bool = False,
                   uploaded: bool = False, now: datetime | None = None) -> dict:
    """Release a staged roster batch (heir letters, heir texts, obituary letters).

    These pieces leave as a file a person uploads, so "release" has two steps and the gate runs at
    both. Without --uploaded, every piece is checked again and the file is written fresh without
    the ones that no longer pass: that is the check that matters, right before the file leaves the
    building. With --uploaded, each piece is written down as sent; one that fails by then has
    already gone, so it is said out loud. Dry unless commit."""
    from . import policy as policy_mod
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    if b["kind"] not in kinds:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=commit)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=commit, out=out)
    texts = b["kind"].endswith("text")
    checked = []
    for it in b["items"]:
        rec = crm.record(it.get("record_uuid") or "")
        checked.append((it, rec, recheck(ctx, rec, dict(it, _batch=batch_id))))
    held = Counter(why for _, _, why in checked if why)
    keep = [it for it, _, why in checked if not why]
    if not commit:
        return {"batch": batch_id, "kind": b["kind"], "pieces": len(b["items"]), "would_hold": sum(held.values()),
                "reasons": dict(held), "usd": b["usd"], "dry": True}
    if not uploaded:
        usd = sum(float(it.get("price") or 0) for it in keep)
        ledger.batch_items_set(batch_id, keep, usd)
        path = export(batch_id, b["kind"], keep)
        if texts:
            for it, _, why in checked:
                if why:
                    limits.drop_text(batch_id, it.get("record_uuid") or "")   # a held text stops holding the owner's day
        if not keep:
            ledger.batch_set(batch_id, "cancelled", {"why": "no piece passed the check at release", "held": dict(held)})
            return {"batch": batch_id, "kind": b["kind"], "pieces": 0, "held": dict(held), "cancelled": True}
        return {"batch": batch_id, "kind": b["kind"], "pieces": len(keep), "held": dict(held), "usd": round(usd, 2),
                "file": path, "next": "upload the file, then run release again with --commit --uploaded"}
    state: dict = {}
    sent, paid = 0, 0.0
    ref = "batch %d" % batch_id
    for it, rec, why in checked:
        if why:
            out.problem("%s was in the uploaded file but no longer passes (%s)" % (it.get("property_street") or "a record", why))
        try:
            record(ctx, rec, it, ref, state)
        except Exception as exc:  # noqa: BLE001  (the file is already uploaded: write down the rest, say what failed)
            out.problem("could not write down the piece for %s (%s)" % (it.get("property_street") or "a record", str(exc)[:120]))
            continue
        if texts:
            limits.release_text(batch_id, it.get("record_uuid") or "", [it.get("number")])
        sent += 1
        paid += float(it.get("price") or 0)
    if texts:
        limits.drop_text(batch_id)          # whatever this batch still holds in the text ledger did not go
    ledger.batch_set(batch_id, "sent", {"sent": sent, "usd": round(paid, 2), "problems": out.problems})
    return {"batch": batch_id, "kind": b["kind"], "sent": sent, "of": len(b["items"]), "usd": round(paid, 2),
            "problems": out.problems}


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Release a batch of heir letters or heir texts."""
    return release_roster(batch_id, (KIND_MAIL, KIND_TEXT), still_due, record_piece, crm=crm, pol=pol, commit=commit,
                          uploaded=uploaded, now=now)


# ---------------------------------------------------------------------- the stage
def options(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k)
        o[k] = v if got is None else got
    return o


def _scope_rows(ctx: Ctx, o: dict, gate_cfg) -> list[dict]:
    """The lane's records: the stage's presets when the account has them, and always the heir tag
    itself, so the lane works before anybody has built its presets."""
    seen, out = set(), []
    cap = int(o["max_scan"])
    for title in ctx.stage.presets:
        for row in researchqueue._preset_rows(ctx, title, cap):
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                out.append(row)
    for tag in (o["scope_tags"] if o["scope_tags"] is not None else gate_cfg.heir_tags):
        tid = ctx.crm.tag_uuid(str(tag))
        if not tid:
            ctx.out.problem("there is no %r tag in this account yet; the research write-back adds it" % tag)
            continue
        for row in ctx.crm.rows({"all_tags": [tid]}, cap=cap):
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                out.append(row)
    return out


def run_heirlane(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    lanes_cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    bodies = o["letter_copy"] if isinstance(o["letter_copy"], list) else None
    copy_problems = audit_pools(bodies)
    for p in copy_problems[:3]:
        ctx.out.problem("heir copy refused: %s. Nothing is staged until the copy is fixed." % p)
    per_heir = int(o["letters_per_heir"])
    if not 3 <= per_heir <= 6:
        ctx.out.problem("letters_per_heir is %d; a verified signer gets three to six letters, so %d is used" % (
            per_heir, min(6, max(3, per_heir))))
        per_heir = min(6, max(3, per_heir))

    who = o["heir_caller"]
    people = resolve_callers(ctx, [who]) if who else resolve_callers(ctx)
    caller = people[0] if people else None
    if not who and caller:
        ctx.out.line("No heir caller is named in the playbook (decision D19); %s is used." % caller["name"])
    if not caller:
        ctx.out.problem("no active heir caller, so nothing is assigned and no call task is written")

    rows = _scope_rows(ctx, o, gate_cfg)
    ctx.out.found = len(rows)
    in_scope = {r["uuid"] for r in rows}
    gone: dict[str, str] = {}
    for b in ledger.batches(status="staged", stage=st.id):
        for it in (ledger.batch(b["id"]) or {}).get("items") or []:
            if it.get("record_uuid") and it["record_uuid"] not in in_scope:
                gone[it["record_uuid"]] = "no longer tagged for the heir lane"

    # A letter to an heir goes to an address the record does not hold, so it can only leave as a file
    # for the mail house: DataSift's own mail addresses the owner on the record.
    vendor = mail.Vendor(dict(st.get("prices") or {}))
    sender = mail.sender_block(st)
    today_iso = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date().isoformat()
    expire_texts(ctx, today_iso)
    lim = limits.settings(ctx.policy)
    texted_owners: set = set()
    staged = staged_keys(st.id, (KIND_MAIL, KIND_TEXT))
    letters: list[dict] = []
    texts: list[dict] = []
    usd = 0.0
    assigned = tasks = 0
    reads = 0
    no_file: list[str] = []
    no_texts: dict[str, str] = {}     # records whose calls and texts are held while their letters may go
    budget_used = False
    for row in rows:
        u = row["uuid"]
        if lanes.status_of(row) in lanes.LEAD_STATUSES:
            ctx.out.tally["now a lead: the lead manager has it"] += 1
            gone[u] = "the record became a lead"
            continue
        if reads >= int(o["max_records"]):
            ctx.out.tally["left for the next run (read cap)"] += 1
            continue
        reads += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        lane = lanes.lane_of(rec, lanes_cfg)
        if lane != "heirs":
            ctx.out.tally["left the heir lane (now %s)" % lane.replace("_", " ")] += 1
            gone[u] = "the record left the heir lane"
            continue
        research = load_research(u, str(o["research_workflow"]))
        if not research:
            ctx.out.tally["tagged, but no research result on this box: left for a person"] += 1
            no_file.append(addr(rec))
            continue
        heirs, notes = heirs_of(research)
        for k, n in notes.items():
            ctx.out.tally[k] += n
        if not heirs:
            ctx.out.tally["no verified signer in the research: nothing to work"] += 1
            continue
        ctx.out.tally["verified signers"] += len(heirs)
        street = (rec.get("address") or {}).get("street") or ""
        city = (rec.get("address") or {}).get("city") or ""

        # ---- calls: the dedicated caller, one heir at a time
        vc = ctx.verdict(rec, "call", row=row)
        usable = {n["number"] for n in vc.numbers} if vc.ok else set()
        turn = {"heir": None, "state": "held", "position": 0, "of": 0}
        if not vc.ok:
            ctx.out.tally["calls and texts held back: %s" % vc.reason()] += 1
            no_texts[u] = vc.reason()
        elif not any(set(x.numbers) & usable for x in heirs):
            ctx.out.tally["no usable number for any signer: letters only"] += 1
        else:
            turn = turn_of(ctx, rec, heirs, usable, o)
        h = turn["heir"]
        if turn["state"] == "done":
            ctx.out.tally["every signer with a number has had a full round: letters only now"] += 1
        elif turn["state"] == "talked":
            ctx.out.tally["a conversation happened: the caller has it"] += 1
        elif turn["state"] == "calling" and h is not None and caller:
            if assignee_of(rec) != caller["uuid"]:
                ctx.change(u, "assign", assignee_of(rec), caller["uuid"])
                ctx.write("heir caller", ctx.crm.assign, u, caller["uuid"])
                assigned += 1
            number = next((n for n in h.numbers if n in usable), "")
            done = ledger.fact(u, TASK_FACT) or {}
            if done.get("heir") != h.key:
                title = "Call %s%s about %s. Number ending %s. Heir %d of %d." % (
                    h.name, (" (%s)" % h.relationship) if h.relationship else "", street, researchroutes.last4(number),
                    turn["position"], turn["of"])
                ctx.change(u, "task", None, title)
                res = ctx.write("heir call task", researchroutes.create_task, ctx.crm, title, property_uuid=u,
                                user_uuid=caller["uuid"], due=researchroutes.end_of_day(ctx.now, ctx.policy.tz),
                                notes="Ask about the property only. Do not bring up a death. One heir at a time: the "
                                      "next heir's turn starts when this one has had a full round.")
                tasks += 1
                ctx.out.line("%s: call %s first (heir %d of %d)" % (addr(rec), h.name, turn["position"], turn["of"]))
                if ctx.live:
                    ledger.set_fact(u, TASK_FACT, {"heir": h.key, "task": str((res or {}).get("uuid") or ""),
                                                   "at": ctx.now.isoformat(timespec="seconds")})
                    ledger.event(ctx.run_id, st.id, u, "heir_turn", {"heir": h.key, "position": turn["position"],
                                                                     "of": turn["of"]}, now=ctx.now, tz=ctx.policy.tz)
            # ---- the text for this heir's turn: by name, about the property
            if o["texts"] and not copy_problems and caller and len(texts) < int(o["daily_texts"]):
                texted = [e for e in ledger.events(u, "heir_text", stage=st.id) if e["detail"].get("heir") == h.key]
                touch = len(texted) + 1
                due = touch <= int(o["texts_per_heir"]) and (
                    not texted or ctx.now - datetime.fromisoformat(texted[-1]["at"]) >= timedelta(days=float(o["text_gap_days"])))
                if due and (u, h.key, KIND_TEXT, touch) not in staged:
                    vt = ctx.verdict(rec, "text", row=row)
                    textable = {n["number"] for n in vt.numbers} if vt.ok else set()
                    tn = next((n for n in h.numbers if n in textable), "")
                    if not h.first:
                        ctx.out.tally["no first name for the heir: no text (a text goes out by name)"] += 1
                    elif not tn:
                        ctx.out.tally["no mobile number of the heir's that may be texted"] += 1
                    else:
                        room_ok, room_why, send_at = text_room(ctx, lim, rec, tn, texted_owners)
                        body = render_text(touch, "%s|%s" % (u, h.key), h.first, street, caller["name"])
                        bad = audit_copy(body, first=h.first, street=street)
                        if not room_ok:
                            ctx.out.tally["no heir text today: %s" % room_why] += 1
                        elif bad:
                            ctx.out.problem("an heir text for %s was refused: it %s" % (addr(rec), bad[0]))
                        else:
                            owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
                            texted_owners.add(owner_uuid)
                            texts.append({"kind": "text", "record_uuid": u, "heir": h.key, "name": h.name,
                                          "first_name": h.first, "number": tn, "touch": touch, "text": body,
                                          "property_street": street, "price": 0.0, "owner_uuid": owner_uuid,
                                          "send_at": send_at, "day": today_iso,
                                          "system": str(o["text_system"])})

        # ---- letters: every verified signer, at their own address
        if copy_problems:
            continue
        vm = ctx.verdict(rec, "mail", row=row)
        if not vm.ok:
            ctx.out.tally["letters held back: %s" % vm.reason()] += 1
            gone[u] = vm.reason()             # whatever is staged for this record comes back out
            continue
        sent = letters_sent(u, st.id)
        for h in heirs:
            n, last = sent.get(h.key, (0, ""))
            if n >= per_heir:
                ctx.out.tally["signers whose letters are finished"] += 1
                continue
            if last and ctx.now - datetime.fromisoformat(last) < timedelta(days=float(o["letter_every_days"])):
                continue
            if (u, h.key, KIND_MAIL, n + 1) in staged:
                ctx.out.tally["already staged, waiting on release"] += 1
                continue
            if not h.mailable():
                ctx.out.tally["signer with no address of their own in the research: no letter"] += 1
                continue
            if len(letters) >= int(o["daily_pieces"]):
                ctx.out.tally["left for tomorrow (daily cap %d)" % int(o["daily_pieces"])] += 1
                continue
            price = vendor.price(str(o["letter_piece"]))
            if budget_used:
                ctx.out.tally["left for tomorrow (the day's mail budget is used)"] += 1
                continue
            ok, why = ctx.budget(usd + price, u, record_usd=0)
            if not ok:
                budget_used = True
                ctx.out.tally["left for tomorrow (%s)" % why] += 1
                continue
            body = render_letter(n + 1, h.first, street, sender.get("name") or (caller or {}).get("name") or "",
                                 sender.get("phone") or "", bodies)
            bad = audit_copy(body, street=street, sms=False)
            if bad:
                ctx.out.problem("an heir letter for %s was refused: it %s" % (addr(rec), bad[0]))
                continue
            usd += price
            letters.append({"kind": "letter", "record_uuid": u, "heir": h.key, "name": h.name, "first_name": h.first,
                            "relationship": h.relationship, "piece": str(o["letter_piece"]), "letter": n + 1,
                            "of": per_heir, "street": h.street, "city": h.city, "state": h.state, "zip": h.zip,
                            "property_street": street, "property_city": city, "text": body, "price": price})

    if no_file:
        ctx.out.problem("%d record(s) carry the heir tag with no research result on this box (for example %s): "
                        "who signs is not guessed" % (len(no_file), ", ".join(no_file[:2])))
    pulled = pull_staged(ctx, (KIND_MAIL, KIND_TEXT), gone) if gone else 0
    held_texts = {u: why for u, why in no_texts.items() if u not in gone}
    pulled += pull_staged(ctx, (KIND_TEXT,), held_texts) if held_texts else 0
    for it in letters[:4]:
        ctx.out.line("%s: letter %d of %d to %s" % (it["property_street"], it["letter"], it["of"], it["name"]))
    ctx.out.acted = assigned + tasks + len(letters) + len(texts) + pulled
    verb = "" if ctx.live else "Would: "
    bits = []
    if assigned:
        bits.append("%d record(s) to %s" % (assigned, caller["name"]))
    if tasks:
        bits.append("%d heir call task(s)" % tasks)
    if letters:
        bits.append("%d letter(s), about $%.2f" % (len(letters), usd))
    if texts:
        bits.append("%d text(s)" % len(texts))
    if pulled:
        bits.append("%d staged piece(s) pulled" % pulled)
    signers = ctx.out.tally.get("verified signers", 0)
    if not ctx.live:
        if letters:
            ctx.reserve(usd)
        ctx.out.head = "%d record(s) in the heir lane, %d verified signer(s). %s%s." % (
            len(rows), signers, verb, ", ".join(bits) if bits else "nothing is due today")
        return
    ids = []
    if letters:
        bid = ledger.batch_create(st.id, KIND_MAIL, letters, usd)
        export(bid, KIND_MAIL, letters)
        ctx.out.batch = bid
        ids.append("letters batch %d" % bid)
    if texts:
        tid = ledger.batch_create(st.id, KIND_TEXT, texts, 0)
        for it in texts:
            # The text ledger holds each owner's place for the day, so no other texting stage picks
            # the owner up while this batch waits on a person.
            limits.stage_text(stage=st.id, batch_id=tid, owner_uuid=it["owner_uuid"], record_uuid=it["record_uuid"],
                              numbers=[{"number": it["number"], "send_at": it["send_at"]}], system=it["system"],
                              series="heir", step=int(it["touch"]), touch="heir:%s:%d" % (it["heir"], int(it["touch"])),
                              tz=ctx.policy.tz, campaign=True, when=ctx.now)
        export(tid, KIND_TEXT, texts)
        ctx.out.batch = ctx.out.batch or tid
        ids.append("texts batch %d" % tid)
    ctx.out.head = "%d record(s) in the heir lane, %d verified signer(s). %s.%s" % (
        len(rows), signers, ("Did: " + ", ".join(bits)) if bits else "Nothing is due today",
        (" Staged for a person: %s (python src/agents/cli.py autopilot release <id>)." % ", ".join(ids)) if ids else "")


def digest_line(stage_id: str) -> str:
    """Letters and texts of this lane that wait on a person, for the morning readout."""
    waiting = [b for b in ledger.batches(status="staged", stage=stage_id) if b["kind"] in (KIND_MAIL, KIND_TEXT)]
    if not waiting:
        return ""
    return "Heir lane: %s wait on a person." % ", ".join(
        "batch %d (%d %s)" % (b["id"], b["count"], "texts" if b["kind"] == KIND_TEXT else "letters") for b in waiting)
