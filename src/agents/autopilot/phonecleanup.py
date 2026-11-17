"""Phone cleanup when the executor is named (Ty, 2026-11-15).

Ty: "Phone cleanup: when a probate filing names the executor, the relatives' numbers from before are
moved down." The course teaches it as a data manager's job and says why it is handled with care (C3.14):
"that has to be manually done by a data manager. That whole process. It's very delicate. Scary to make
mass changes like that." It came up after a pre-probate skip trace mixed the relatives' numbers with the
executor's numbers that a later probate upload brought in (C11.7).

What goes wrong without it. Research on a pre-probate record writes the family's numbers onto the owner,
each tagged with the relationship (Son, Daughter, Brother), a tier and a source. Weeks later the probate
filing names the executor and the uploader makes that person the owner of record. The phone list the
caller works now has a brother's Dial First number above the executor's own, so the family is dialed
before the one person the court put in charge.

The rules, each one a way this could go wrong:

  * WHO IS NAMED. The executor comes from the record's own personal representative (the probate filing),
    then the custom fields in `pr_fields`, in order (Personal Representative, then Decision Maker). A
    value that names a court, a law firm, an estate or a group names nobody. A field that names two
    people in a way that cannot be read is left for a person, never half-read. A Decision Maker the
    record marks low confidence (`hold_confidence`) is not taken as named.
  * RESEARCH HAS THE LAST WORD ON A PERSON. When research on this box named the executor and did not show
    them living (UNVERIFIED, Unknown or Deceased), the record is left alone and listed for a person: a
    phone list is not reordered around somebody research could not confirm is alive. The same when the
    executor field holds the name of the person who died.
  * WHOSE NUMBER IT IS. A phone carries no name (a name never goes in a phone tag), so a number is the
    executor's when the record's PR phone field holds it, when it carries a role tag (Executor, Decision
    Maker), or when research's call order, trace or name check put it under the executor. A relative's
    number moves down only when something says it is NOT the executor's: research put it under somebody
    else, or the executor's relationship is known and the number's relationship tag names a different one
    (a daughter's number is not the son's). A son's number when a son is the executor could be his, so it
    stays and the record is listed for a person. Names are compared wide on the side of keeping (a
    nickname, a married name: the same first name is enough) and strict when a relationship is borrowed
    from one person for another.
  * NEVER A PERSON'S CALL. A number whose status a person or the dialer set (CORRECT, WRONG, DNC, no
    answer ...) is never touched, nor one marked do not call. A number is never raised: one already at
    the lower tier or below, or with no tier yet, stays. A number this stage moved that somebody raised
    again stays where they put it.
  * NOTHING DELETED. A number is moved, never removed: its tier tag becomes `lower_tier` (Dial Fourth)
    and it gains `why_tag` (Before Executor), so the caller sees why and the family can still be reached.
    Every move records the tags the numbers carried before (the ledger change "phones"), and
    `autopilot undo <run>` puts them back.
  * CO-SIGNERS. With only a decision maker named (no court executor yet) every heir may still have to
    sign, so the record's other decision makers and research's verified signers keep their numbers too
    (`keep_signers: decision_maker`, the default; `always` keeps them under a court executor as well,
    `never` keeps only the named person's).
  * NOT A MASS CHANGE. At most `max_records` records are changed a run and at most `max_reads` read,
    newest first: the record that most recently joined a list or gained a tag (read from the shared log
    copy) comes first. The first live write is read back, and a write that did not take stops the run.

The gate's research channel decides whether a record may be touched: Do Not Market, a litigator, an
opt-out, a sold house and a finished status all hold it, while the death that put the record here does
not. A lead is never touched (the lead manager has it), nor a record whose research is still running,
nor one owned by a company or a trust (people only).

Each record's verdict is remembered (a ledger fact) with the search row's fingerprint and the research
task it was judged on, so it is not read again until one of them changes. A number added to the owner
leaves no mark on a search row, so every record is read again after `recheck_days`. A verdict a plan
run reached is never trusted by a live run: a plan writes nothing, so the work is still there.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .. import config, store
from ..research import writeback as wb
from . import activity, gate, heirlane, lanes, ledger, researchqueue
from .stages import (CALLABLE_TIERS, GONE_STATUSES, TIER_NAMES, TIER_RANK, Ctx, addr, mask, phone_payload,
                     phone_tier, tag_titles)

log = logging.getLogger(__name__)

KIND = "phonecleanup"
FACT = "phonecleanup_verdict"         # what this stage decided about a record, and on what it decided
UNSET = {"", "UNKNOWN", "NONE", "NULL"}    # a phone status nobody and no dialer has set
DO_NOT_CALL = {"dnc", "litigator"}     # phone tags that mark a number not to be dialed at all
RECENT_DAYS = 90                       # how far back the log copy is read to put the newest records first
KEEP_SIGNERS = ("decision_maker", "always", "never")
OPEN_TASKS = ("queued", "running", "waiting")
ENTITY_WHY = "a company or a trust owns it, or the owner has no full name: people only"
FILE_COLS = ["record", "street", "executor", "field", "why", "numbers"]

DEFAULTS = {
    "scope_lists": ["Probate", "Obituary"],
    "scope_tags": ["Deep Prospected", "Heir Identified"],
    "pr_fields": ["Personal Representative", "Decision Maker"],
    "pr_phone_fields": ["Personal Representative Phone"],
    # A name field and the field that holds that person's relationship to the owner.
    "relationship_fields": {"Decision Maker": "DM Relationship", "DM 2 Name": "DM 2 Relationship",
                            "DM 3 Name": "DM 3 Relationship"},
    "decedent_field": "Decedent Name",
    "confidence_field": "DM Confidence",
    "hold_confidence": ["low", "none"],
    "lower_tier": "Dial Fourth",
    "why_tag": "Before Executor",
    "relative_tags": None,            # default: every word in KIN, plus "in law"
    "executor_tags": ["Executor", "Executrix", "Personal Representative", "Administrator", "Administratrix",
                      "PR", "Decision Maker"],
    "keep_signers": "decision_maker",
    "research_workflow": "deep_prospect",
    "max_reads": 150,
    "max_records": 25,
    "max_scan": 3000,
    "recheck_days": 14,
    "max_log_calls": 60,              # enough to read the 45 days a fresh box's first log sync reaches back
}

# word -> (family, which member): "" when the word does not say which one, so it could be anybody in the
# family. Two relationships name different people only when the families differ (a brother is not a
# son) or two named members of one family differ (a daughter is not a son).
KIN = {
    "son": ("child", "son"), "daughter": ("child", "daughter"), "child": ("child", ""), "children": ("child", ""),
    "stepson": ("child", "son"), "stepdaughter": ("child", "daughter"), "stepchild": ("child", ""),
    "wife": ("spouse", "wife"), "husband": ("spouse", "husband"), "spouse": ("spouse", ""),
    "widow": ("spouse", "wife"), "widower": ("spouse", "husband"), "partner": ("spouse", ""),
    "fiance": ("spouse", ""), "fiancee": ("spouse", ""),
    "brother": ("sibling", "brother"), "sister": ("sibling", "sister"), "sibling": ("sibling", ""),
    "mother": ("parent", "mother"), "father": ("parent", "father"), "parent": ("parent", ""),
    "mom": ("parent", "mother"), "dad": ("parent", "father"),
    "grandson": ("grandchild", "grandson"), "granddaughter": ("grandchild", "granddaughter"),
    "grandchild": ("grandchild", ""), "grandchildren": ("grandchild", ""),
    "grandmother": ("grandparent", "grandmother"), "grandfather": ("grandparent", "grandfather"),
    "grandparent": ("grandparent", ""),
    "nephew": ("nephew or niece", "nephew"), "niece": ("nephew or niece", "niece"),
    "aunt": ("aunt or uncle", "aunt"), "uncle": ("aunt or uncle", "uncle"),
    "cousin": ("cousin", ""),
    "relative": ("relative", ""), "relation": ("relative", ""), "family": ("relative", ""), "kin": ("relative", ""),
    "heir": ("relative", ""),
}

# Common nicknames, so "Pat" and "Patrick" or "Bill" and "William" are never read as two different people
# when a number is about to be moved away from the executor.
NICKNAMES = [
    {"william", "bill", "billy", "will", "willie"}, {"robert", "bob", "bobby", "rob", "robbie"},
    {"richard", "dick", "rick", "ricky", "rich", "richie"}, {"james", "jim", "jimmy", "jamie"},
    {"john", "jack", "johnny", "jon"}, {"joseph", "joe", "joey"}, {"thomas", "tom", "tommy"},
    {"michael", "mike", "mikey", "mick"}, {"charles", "charlie", "chuck"}, {"edward", "ed", "eddie", "ted", "ned"},
    {"henry", "hank", "harry"}, {"lawrence", "larry"}, {"anthony", "tony"},
    {"margaret", "peggy", "maggie", "meg", "marge"}, {"elizabeth", "liz", "beth", "betty", "betsy", "lisa"},
    {"katherine", "catherine", "kathryn", "kathy", "kate", "katie", "cathy", "kay"}, {"patricia", "pat", "patty", "trish"},
    {"patrick", "pat"}, {"deborah", "debra", "debbie", "deb"}, {"susan", "sue", "suzy"}, {"barbara", "barb"},
    {"jennifer", "jen", "jenny"}, {"rebecca", "becky", "becca"}, {"cynthia", "cindy"}, {"sandra", "sandy"},
    {"dorothy", "dot", "dottie"}, {"frances", "fran"}, {"francis", "frank"}, {"gerald", "jerry"},
    {"donald", "don", "donnie"}, {"ronald", "ron", "ronnie"}, {"kenneth", "ken", "kenny"}, {"leonard", "len", "lenny"},
    {"alfred", "al", "alf", "fred"}, {"frederick", "fred", "freddie"}, {"albert", "al", "bert"},
    {"theodore", "ted", "teddy", "theo"}, {"eugene", "gene"}, {"raymond", "ray"}, {"walter", "walt"},
    {"harold", "hal", "harry"}, {"nancy", "nan"}, {"mary", "molly", "polly"}, {"carolyn", "caroline", "carol", "carrie"},
    {"judith", "judy"}, {"victoria", "vicky", "tori"}, {"christopher", "chris", "kit"},
    {"christine", "christina", "chris", "tina"},
]
GENERATION = {"jr", "sr", "ii", "iii", "iv"}

# A value in an executor field that names nobody: a court, a clerk, a firm, an estate, a group, a blank.
NOT_A_PERSON = re.compile(
    r"\b(court|chancery|probate|clerk|attorneys?|law|firm|offices?|esq|pllc|llc|inc|corp|company|bank|county|"
    r"state|city|estate|heirs?|unknown|n/?a|none|pending|trusts?|trustees?|executors?|executrix|administrat\w*|"
    r"representatives?|devisees?|beneficiar\w*|successors?)\b", re.I)
LEAD_WORDS = {"by", "from", "of", "to", "for", "per", "co", "the", "care", "attn"}
COURT_LABEL = re.compile(r"representative|executor|executrix|administrat", re.I)
ROLE_RX = re.compile(r"\b(executor|executrix|personal representative|administrator|administratrix)\b", re.I)
GROUP_RX = re.compile(r"\b(heirs?|estate|family|children|kids|unknown|whoever|next of kin|beneficiar\w*|devisees?)\b", re.I)


# ---------------------------------------------------------------------- settings
def options(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k) if hasattr(st, "get") else None
        o[k] = v if got is None else got
    return o


def _strings(v) -> bool:
    return isinstance(v, (list, tuple)) and all(isinstance(x, str) and x.strip() for x in v)


def block_problems(st) -> list[str]:
    """What is wrong with this stage's block, read when the playbook loads, so a typo is refused instead
    of read as a default. A lower tier that is still dialed would leave the relatives in the dial lane
    with a new tag on, which is the problem this stage exists to fix."""
    o = options(st)
    p = []
    lower = o["lower_tier"]
    if lower not in TIER_NAMES:
        p.append("stage %s: lower_tier %r is not a tier (%s)" % (st.id, lower, ", ".join(TIER_NAMES)))
    elif lower in CALLABLE_TIERS:
        p.append("stage %s: lower_tier %s is still dialed; a number moved down must leave the dial lane "
                 "(use Dial Third, Dial Fourth or Drop)" % (st.id, lower))
    for k in ("scope_lists", "scope_tags", "pr_fields", "pr_phone_fields", "hold_confidence", "executor_tags"):
        if not isinstance(o[k], (list, tuple)) or (o[k] and not _strings(o[k])):
            p.append("stage %s: %s must be a list of names" % (st.id, k))
    relatives_ok = o["relative_tags"] is None or (_strings(o["relative_tags"]) and bool(o["relative_tags"]))
    if not relatives_ok:
        p.append("stage %s: relative_tags must be a list of relationship words (leave it out for the built-in list)" % st.id)
    why = o["why_tag"]
    if not isinstance(why, str) or not why.strip():
        p.append("stage %s: why_tag must name the tag that says why a number moved" % st.id)
    elif why.strip() in TIER_NAMES:
        p.append("stage %s: why_tag %r is a tier; it must be its own tag" % (st.id, why))
    elif _strings(o["executor_tags"]) and why.strip().lower() in {t.strip().lower() for t in o["executor_tags"]}:
        # A moved number carrying a role tag would read, on the next run, as the executor's own.
        p.append("stage %s: why_tag %r is one of the executor_tags; a moved number would read as the executor's" % (st.id, why))
    elif relatives_ok and relation_in(why, vocab(o)):
        p.append("stage %s: why_tag %r reads as a relationship; it must say why a number moved" % (st.id, why))
    if not (isinstance(o["pr_fields"], (list, tuple)) and o["pr_fields"]):
        p.append("stage %s: pr_fields must name at least one field the executor is read from" % st.id)
    if not o["scope_lists"] and not o["scope_tags"]:
        p.append("stage %s: name at least one list or tag to read records from" % st.id)
    rf = o["relationship_fields"]
    if not isinstance(rf, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in rf.items()):
        p.append("stage %s: relationship_fields must map a name field to the field that holds that person's relationship" % st.id)
    for k in ("decedent_field", "confidence_field", "research_workflow"):
        if not isinstance(o[k], str):
            p.append("stage %s: %s must be a field or workflow name" % (st.id, k))
    if o["keep_signers"] not in KEEP_SIGNERS:
        p.append("stage %s: keep_signers must be %s" % (st.id, ", ".join(KEEP_SIGNERS)))
    for k, least in (("max_reads", 1), ("max_records", 0), ("max_scan", 1), ("recheck_days", 1), ("max_log_calls", 0)):
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, int) or v < least:
            p.append("stage %s: %s must be a whole number of at least %d" % (st.id, k, least))
    return p


def vocab(o: dict) -> set:
    """The relationship words a phone tag may carry, in lower case."""
    words = o.get("relative_tags")
    base = list(KIN) + ["in law"] if words is None else list(words)
    return {" ".join(str(w).lower().replace("-", " ").split()) for w in base if str(w).strip()}


# ---------------------------------------------------------------------- relationships
def relation(phrase: str, words: set) -> tuple[str, str] | None:
    """(family, which member) for a phrase that IS a relationship word ("Son", "step daughter",
    "Daughter-in-law", "Brothers"), or None."""
    s = " ".join(str(phrase or "").lower().replace("-", " ").split())
    if not s:
        return None
    m = re.fullmatch(r"(?:([a-z]+) )?in law", s)
    if m:
        return ("in law", m.group(1) or "") if "in law" in words else None
    toks = s.split()
    while len(toks) > 1 and toks[0] in ("step", "half", "great"):
        toks = toks[1:]
    if len(toks) > 1 and toks[0] == "grand":
        toks = ["grand" + toks[1]] + toks[2:]     # "Grand Son" is a grandson, never a son
    base = " ".join(toks)
    if base not in KIN and base.endswith("s") and base[:-1] in KIN:
        base = base[:-1]
    if base in words:
        return KIN.get(base, (base, ""))
    return None


def relation_in(text: str, words: set) -> tuple[str, str] | None:
    """The first relationship a short text names ("daughter (named in the obituary)", "STATUS
    UNVERIFIED (...). son"), or None."""
    s = re.sub(r"status unverified\s*\([^)]*\)\.?", " ", str(text or "").lower())
    toks = re.sub(r"[^a-z]+", " ", s.replace("-", " ")).split()
    for i in range(len(toks)):
        for size in (3, 2, 1):
            if i + size <= len(toks):
                r = relation(" ".join(toks[i:i + size]), words)
                if r:
                    return r
    return None


def differs(a, b) -> bool:
    """True only when two relationships cannot be one person. A word that does not say which one
    (child, relative) could be anybody in its family, and a missing relationship could be anybody."""
    if not a or not b:
        return False
    if a[0] == "relative" or b[0] == "relative":
        return False
    if a[0] != b[0]:
        return True
    return bool(a[1] and b[1] and a[1] != b[1])


# ---------------------------------------------------------------------- names
def _gen(name: str) -> str:
    return next((t for t in re.sub(r"[^a-z ]", " ", str(name or "").lower()).split() if t in GENERATION), "")


def same(a, b) -> bool:
    """Two spellings of one person, by either test the house already uses: the write-back's (it reads a
    county "Last, First" and keeps a Sr. apart from his Jr.) and the heir lane's (a lone first name
    matches the full name it starts)."""
    a, b = wb.plain(a), wb.plain(b)
    if not wb.name_tokens(a) or not wb.name_tokens(b):
        return False
    return wb.same_name(a, b) or heirlane.same_person(a, b)


def _first_names_agree(x: str, y: str) -> bool:
    if x == y or (len(x) >= 3 and len(y) >= 3 and x[:3] == y[:3]):
        return True
    return any(x in g and y in g for g in NICKNAMES)


def maybe_same(a, b) -> bool:
    """Could these be one person? Used only to KEEP a number where it is, so it errs wide: the same
    first name or a nickname of it is enough (a married daughter's surname changes, and the court and
    the research may not spell her alike), unless one is a senior and the other a junior."""
    if same(a, b):
        return True
    ga, gb = _gen(a), _gen(b)
    if ga and gb and ga != gb:
        return False
    ta, tb = wb.name_tokens(wb.plain(a)), wb.name_tokens(wb.plain(b))
    if not ta or not tb:
        return False
    return _first_names_agree(ta[0], tb[0])


def _one_person(p: str) -> bool:
    words = p.replace(",", " ").split()
    if not 2 <= len(words) <= 6 or words[0].lower().strip(".") in LEAD_WORDS:
        return False
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", w) for w in words):
        return False
    return len(wb.name_tokens(p)) >= 2


def person_names(value) -> tuple[list[str], bool]:
    """(the people a field names, whether it names people in a way that cannot be read). A value naming
    a court, a firm, an estate or "Unknown" names nobody. Two executors in one field ("Pat & Robin
    Example") are two people sharing the surname; a part that is still not a full name makes the whole
    field unclear, because reading half of it would leave a co-executor's numbers unprotected."""
    s = wb.plain(value)
    if not s or re.search(r"\d", s) or NOT_A_PERSON.search(s):
        return [], False
    parts = [x.strip(" ,") for x in re.split(r"\s*(?:&|;|/|\+|\band\b)\s*", s, flags=re.I) if x.strip(" ,")]
    if not parts:
        return [], False
    names = []
    for i, part in enumerate(parts):
        if len(part.replace(",", " ").split()) == 1 and i + 1 < len(parts):
            tail = parts[-1].replace(",", " ").split()
            if len(tail) >= 2:
                part = "%s %s" % (part, tail[-1])
        if not _one_person(part):
            return [], len(parts) > 1
        names.append(part)
    return names, False


def _lower(custom: dict | None) -> dict:
    return {str(k).strip().lower(): v for k, v in (custom or {}).items()}


def named_person(rec: dict, custom: dict | None, o: dict) -> dict | None:
    """Who the record names as executor or decision maker: the first field that names a person, as
    {"names", "field", "court", "unclear"}; None when no field names anybody."""
    low = _lower(custom)
    fields = [("the record's personal representative", rec.get("personal_representative"), True)]
    fields += [("the %s field" % label, low.get(str(label).strip().lower()), bool(COURT_LABEL.search(str(label))))
               for label in o["pr_fields"]]
    for label, value, court in fields:
        names, unclear = person_names(value)
        if unclear:
            return {"names": [], "field": label, "court": court, "unclear": True}
        if names:
            return {"names": names, "field": label, "court": court, "unclear": False}
    return None


def relation_of(name: str, custom: dict | None, research: dict | None, o: dict, words: set):
    """This person's relationship to the owner, when the record or the research says it, else None. It
    is borrowed only from an entry that is plainly the same person (not merely the same first name), and
    two sources that disagree make it unknown: a wrong relationship would move this person's own
    number."""
    found = []
    for p in (research or {}).get("people") or []:
        if isinstance(p, dict) and same(p.get("name"), name):
            r = relation_in(p.get("tag"), words) or relation_in(p.get("relationship"), words)
            if r:
                found.append(r)
    low = _lower(custom)
    for name_field, rel_field in (o["relationship_fields"] or {}).items():
        other = wb.plain(low.get(str(name_field).strip().lower()))
        if other and same(other, name):
            r = relation_in(low.get(str(rel_field).strip().lower()), words)
            if r:
                found.append(r)
    found = [r for r in found if r[0] != "relative"] or found     # "relative" adds nothing to "son"
    if not found:
        return None
    best = max(found, key=lambda r: bool(r[1]))
    if any(differs(best, r) for r in found):
        return None
    return best


def decedent_clash(names: list[str], rec: dict, custom: dict | None, o: dict) -> bool:
    """Does the executor field hold the name of the person who died? (The Decedent Name field, or the
    owner the record marks deceased.) A parsed notice that put the decedent in the executor's place
    would otherwise reorder the numbers around a dead man's name."""
    dead = [wb.plain(_lower(custom).get(str(o["decedent_field"]).strip().lower()))]
    ow = rec.get("owner") or {}
    if ow.get("deceased"):
        dead.append(wb.owner_label(ow))
    return any(d and same(d, n) for d in dead for n in names)


# ---------------------------------------------------------------------- what research says
def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def research_view(record_uuid: str, o: dict) -> dict | None:
    """What research on this box found on the record (the heir lane's reader), plus the analysis's
    also_reaches: a number Trestle confirmed also reaches a person SmartSkip did not file it under."""
    workflow = str(o["research_workflow"])
    research = heirlane.load_research(record_uuid, workflow)
    if not research:
        return None
    also: dict = {}
    res = researchqueue.last_result(record_uuid, workflow) or {}
    run_dir = Path(str(res.get("run_dir"))) if res.get("run_dir") else None
    if run_dir is not None and research.get("slug"):
        an = _read_json(run_dir / "analysis" / (research["slug"] + ".json"))
        ar = an.get("also_reaches") if isinstance(an, dict) else None
        if isinstance(ar, dict):
            also = {gate.digits(k): [str(x) for x in (v or [])] for k, v in ar.items()}
        elif isinstance(ar, list):
            also = {gate.digits(e.get("number")): [str(x) for x in (e.get("names") or [])] for e in ar if isinstance(e, dict)}
    research["also_reaches"] = {k: v for k, v in also.items() if k}
    return research


def research_objection(names: list[str], research: dict | None) -> str:
    """Why research holds the record, or "": research named the executor and did not show them living."""
    if not research:
        return ""
    for m in research.get("must_sign") or []:
        if not isinstance(m, dict) or not str(m.get("status") or "").strip().upper().startswith("UNVERIFIED"):
            continue
        for part in re.split(r",|;|&|\band\b", str(m.get("who") or "")):
            if any(same(part, n) for n in names):
                return "research could not confirm the named executor is living"
    for n in names:
        states = {str(p.get("living") or "") for p in research.get("people") or [] if isinstance(p, dict) and same(p.get("name"), n)}
        if states and "Living" not in states:
            return ("research shows the named executor deceased" if "Deceased" in states
                    else "research could not confirm the named executor is living")
    return ""


def research_numbers(research: dict | None) -> tuple[dict, set]:
    """(number -> the names research put it under, the numbers research put under the executor's role).
    From the call order, the trace (each SmartSkip person's own numbers) and the name check."""
    out: dict[str, set] = {}
    role: set = set()
    if not research:
        return out, role

    def put(number, name) -> None:
        n, nm = gate.digits(number), wb.plain(name)
        if not n or not nm:
            return
        if ROLE_RX.search(nm):
            role.add(n)                   # "the executor" in a call order names the executor's number
            return
        if wb.name_tokens(nm) and not GROUP_RX.search(nm):
            out.setdefault(n, set()).add(nm)

    for c in research.get("call_order") or []:
        if isinstance(c, dict):
            put(c.get("number"), c.get("who"))
    by_key = {p.get("ss_key"): p for p in research.get("people") or [] if isinstance(p, dict) and p.get("ss_key")}
    for key, sp in (research.get("index") or {}).items():
        if not isinstance(sp, dict):
            continue
        names = [sp.get("name"), (by_key.get(key) or {}).get("name")]
        for q in sp.get("phones") or []:
            number = q.get("number") if isinstance(q, dict) else q
            for nm in names:
                if nm:
                    put(number, nm)
    for n, names in (research.get("also_reaches") or {}).items():
        for nm in names:
            put(n, nm)
    return out, role


def protected_people(named: dict, custom: dict | None, research: dict | None, o: dict, words: set) -> list[dict]:
    """Everyone whose numbers stay: the named executor or decision maker and, by the signer rule, the
    record's other decision makers and the research's verified signers. [{"name", "relation"}]"""
    people = [{"name": n, "relation": relation_of(n, custom, research, o, words)} for n in named["names"]]
    mode = str(o["keep_signers"])
    if mode == "always" or (mode == "decision_maker" and not named["court"]):
        low = _lower(custom)
        for name_field in (o["relationship_fields"] or {}):
            names, _ = person_names(low.get(str(name_field).strip().lower()))
            for n in names:
                if not any(same(n, q["name"]) for q in people):
                    people.append({"name": n, "relation": relation_of(n, custom, research, o, words)})
        if research:
            heirs, _ = heirlane.heirs_of(research)
            for h in heirs:
                if not any(same(h.name, q["name"]) for q in people):
                    people.append({"name": h.name, "relation": relation_in(h.relationship, words)
                                   or relation_of(h.name, custom, research, o, words)})
    return people


# ---------------------------------------------------------------------- the numbers
def _phone_marks(p: dict, o: dict, words: set) -> tuple[list, set]:
    """(the relationships a phone's tags name, its tags in lower case)."""
    tags = tag_titles(p)
    rels = [r for r in (relation_in(t, words) for t in tags) if r]
    return rels, {t.strip().lower() for t in tags}


def _movable(p: dict, o: dict, words: set) -> bool:
    """A relative's number this stage could move at all, before anyone is named: a relationship tag, no
    status, no do-not-call mark, a tier above the lower tier and not moved before. Records with none are
    passed over without reading anything more."""
    rels, low = _phone_marks(p, o, words)
    if not rels or low & {t.lower() for t in o["executor_tags"]} or low & DO_NOT_CALL:
        return False
    if str(p.get("status") or "UNKNOWN").strip().upper() not in UNSET or str(o["why_tag"]).lower() in low:
        return False
    tier = phone_tier(p)
    return bool(tier) and TIER_RANK.get(tier, 9) < TIER_RANK[o["lower_tier"]]


def sort_numbers(rec: dict, people: list[dict], rmap: dict, pr_phones: set, o: dict, words: set) -> dict:
    """Which of the owner's numbers move down, which stay and why, and which could be the executor's own
    (those stay, and the record is listed for a person). Only numbers with a relationship tag are
    looked at: the owner's own line carries none (the owner rule)."""
    executors = {t.lower() for t in o["executor_tags"]}
    lower, why = o["lower_tier"], str(o["why_tag"]).lower()
    move, unsure = [], []
    kept: Counter = Counter()
    for p in (rec.get("owner") or {}).get("phones") or []:
        n = gate.digits(p.get("number"))
        rels, low = _phone_marks(p, o, words)
        if not n or not (rels or low & executors):
            continue
        if low & executors or n in pr_phones:
            kept["of the executor's own"] += 1
            continue
        if str(p.get("status") or "UNKNOWN").strip().upper() not in UNSET:
            kept["with a status a person or the dialer set"] += 1
            continue
        if low & DO_NOT_CALL:
            kept["marked do not call"] += 1
            continue
        tier = phone_tier(p)
        if not tier:
            kept["with no tier yet"] += 1
            continue
        below = TIER_RANK.get(tier, 9) >= TIER_RANK[lower]
        if why in low:
            kept["moved on an earlier run" if below else "raised again by a person after an earlier move"] += 1
            continue
        if below:
            kept["already at %s or lower" % lower] += 1
            continue
        names = rmap.get(n) or set()
        if names:
            if any(maybe_same(x, q["name"]) for x in names for q in people):
                kept["that research puts under the executor or a signer"] += 1
                continue
            move.append(p)
            continue
        known = [q.get("relation") for q in people]
        if people and all(known) and all(differs(r, k) for r in rels for k in known):
            move.append(p)
            continue
        unsure.append(p)
        kept["that could be the executor's own (left for a person)"] += 1
    return {"move": move, "unsure": unsure, "kept": kept}


def dial_left(rec: dict, moves: list[dict], o: dict, cfg) -> int:
    """How many numbers a caller could still dial once the moves are made (the gate's own rule for a
    call). Zero means the executor has no number on the record yet and needs a skip trace."""
    moved = {gate.digits(p.get("number")) for p in moves}
    after = copy.deepcopy(rec)
    for p in (after.get("owner") or {}).get("phones") or []:
        if gate.digits(p.get("number")) in moved:
            p["tags"] = [t for t in tag_titles(p) if t not in TIER_NAMES] + [o["lower_tier"], o["why_tag"]]
    return len(gate.usable_numbers(after, "call", cfg))


# ---------------------------------------------------------------------- one record
def _entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return is_entity(owner or {})


def fresh_custom(ctx: Ctx, u: str) -> dict | None:
    """The record's custom fields as they are now, or None when they cannot be read. Not the gate's
    week-old copy: a record is read here because it changed, and the change is usually a probate upload
    that just wrote the executor's name into a custom field. The copy is stored where the gate keeps its
    own, so the gate reads this one and the read costs nothing extra."""
    try:
        c = ctx.crm.custom_fields(u)
    except Exception as exc:  # noqa: BLE001  (said, and the record is looked at again next run)
        ctx.out.problem("could not read a record's custom fields (%s), so its executor was not checked" % str(exc)[:80])
        return None
    ledger.set_fact(u, "custom_fields", c)
    return c


def judge(ctx: Ctx, rec: dict, row: dict | None, o: dict, words: set) -> dict:
    """What this stage would do to one full record, writing nothing. verdict: move (some numbers go
    down), held (a reason keeps the record as it is), none (nothing here to move) or retry (a read
    failed, so nothing is decided and the record is read again next run)."""
    v = {"verdict": "none", "why": "", "move": [], "unsure": [], "kept": Counter(), "names": [], "field": "",
         "person": "", "street": addr(rec), "dial_left": None}
    u = rec.get("uuid") or ""
    if not u:
        v["why"] = "the record could not be read"
        return v
    if lanes.status_of(rec) in lanes.LEAD_STATUSES:
        v["why"] = "a lead: the lead manager has it"
        return v
    if _entity(rec.get("owner") or {}):
        v["why"] = ENTITY_WHY
        return v
    if not any(_movable(p, o, words) for p in (rec.get("owner") or {}).get("phones") or []):
        v["why"] = "no relative's number above %s" % o["lower_tier"]
        return v
    custom = fresh_custom(ctx, u)
    if custom is None:
        v.update(verdict="retry", why="its custom fields could not be read: looked at again next run")
        return v
    if not ctx.passes(rec, "research", row=row):
        v["verdict"] = "held"                 # the gate already counted its reason
        return v
    named = named_person(rec, custom, o)
    if not named:
        v["why"] = "no executor or decision maker named yet"
        return v
    v["field"] = named["field"]
    if named["unclear"]:
        v.update(verdict="held", why="the executor field names people in a way that cannot be read",
                 person="%s names more than one person in a way that cannot be read" % named["field"])
        return v
    v["names"] = named["names"]
    hold = {str(x).strip().lower() for x in o["hold_confidence"]}
    conf = str(_lower(custom).get(str(o["confidence_field"]).strip().lower()) or "").strip().lower()
    if not named["court"] and conf in hold:
        v.update(verdict="held", why="the decision maker on the record is marked %s confidence" % conf)
        return v
    if decedent_clash(named["names"], rec, custom, o):
        v.update(verdict="held", why="the executor field holds the name of the person who died",
                 person="the executor field holds the name of the person who died")
        return v
    research = research_view(u, o)
    objection = research_objection(named["names"], research)
    if objection:
        v.update(verdict="held", why=objection, person=objection)
        return v
    people = protected_people(named, custom, research, o, words)
    rmap, role = research_numbers(research)
    low = _lower(custom)
    pr_phones = {gate.digits(rec.get("personal_representative_phone"))} | role
    pr_phones |= {gate.digits(low.get(str(f).strip().lower())) for f in o["pr_phone_fields"]}
    pr_phones.discard("")
    s = sort_numbers(rec, people, rmap, pr_phones, o, words)
    v.update(move=s["move"], unsure=s["unsure"], kept=s["kept"])
    if s["move"]:
        v["dial_left"] = dial_left(rec, s["move"], o, getattr(ctx.policy, "gate", None) or gate.GateConfig())
    if s["unsure"]:
        v["person"] = "relatives' numbers that could be the executor's own"
    if s["move"]:
        v["verdict"] = "move"
    elif s["unsure"]:
        v.update(verdict="held", why="relatives' numbers that could be the executor's own")
    else:
        v["why"] = "every relative's number stays where it is"
    return v


def write_moves(ctx: Ctx, rec: dict, moves: list[dict], o: dict, state: dict) -> bool:
    """Move the numbers down in one upsert, after recording the tags they carried (undo puts those
    back). Only the moved numbers are sent: upsert-phones replaces a phone by its number and leaves the
    rest. The first live write is read back; one that did not take stops the run."""
    u = rec.get("uuid") or ""
    owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
    if not owner_uuid:
        ctx.out.problem("a record with numbers to move has no owner id, so nothing was moved on it (%s)" % addr(rec))
        return False
    once: dict = {}
    for p in moves:                            # one number twice on an owner would make the whole upsert refuse
        once.setdefault(gate.digits(p.get("number")) or str(p.get("number")), p)
    moves = list(once.values())
    payload = [phone_payload(p, o["lower_tier"], [o["why_tag"]]) for p in moves]
    before = [phone_payload(p, phone_tier(p)) for p in moves]
    ctx.change(u, "phones", before, [ph["number"] for ph in payload])
    try:
        ctx.write("move relatives' numbers down", ctx.crm.upsert_phones, owner_uuid, payload)
    except Exception as exc:  # noqa: BLE001  (say it, and stop: a phone change is not retried blind)
        state["broken"] = True
        ctx.out.problem("a phone change was refused on %s (%s); nothing more was moved this run" % (addr(rec), str(exc)[:100]))
        return False
    if ctx.live and not state.get("checked"):
        state["checked"] = True
        back: dict = {}
        for p in (ctx.crm.record(u).get("owner") or {}).get("phones") or []:
            back.setdefault(gate.digits(p.get("number")), []).append(p)
        for ph in payload:
            copies = back.get(ph["number"]) or []
            if not any(set(ph["tags"]) <= set(tag_titles(q)) for q in copies):
                state["broken"] = True
                ctx.out.problem("a phone change did not take on %s (wrote %s, read back %s); nothing more was moved this run"
                                % (addr(rec), o["lower_tier"], phone_tier(copies[0]) if copies else "no number"))
                return False
    return True


# ---------------------------------------------------------------------- reading the scope
def _scope_rows(ctx: Ctx, o: dict) -> list[dict]:
    """The records on the scope lists and with the scope tags, each once, finished records left out by the
    search itself (the gate holds them anyway)."""
    seen, out = set(), []
    cap = int(o["max_scan"])
    not_gone = {"must_not": {"any_property_status": list(GONE_STATUSES)}}

    def add(rows) -> None:
        for row in rows:
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                out.append(row)

    for title in o["scope_lists"]:
        lid = ctx.crm.list_uuid(str(title))
        if not lid:
            ctx.out.problem("there is no %r list in this account, so no record is read from it" % title)
            continue
        add(ctx.crm.rows(dict({"any_lists": [lid]}, **not_gone), cap=cap))
    for title in o["scope_tags"]:
        tid = ctx.crm.tag_uuid(str(title))
        if not tid:
            ctx.out.problem("there is no %r tag in this account, so no record is read by it" % title)
            continue
        add(ctx.crm.rows(dict({"all_tags": [tid]}, **not_gone), cap=cap))
    return out


def newest_first(rows: list[dict], now: datetime) -> list[dict]:
    """The record that most recently joined a list or gained a tag first, when the shared log copy holds
    it (a probate upload adds the Probate list and the notice's tags); the rest keep their search order."""
    last: dict = {}
    for e in activity.events(("property.lists.added", "property.tags.added"), since=now - timedelta(days=RECENT_DAYS)):
        r = e.get("resource") or ""
        if r and (r not in last or e["when"] > last[r]):
            last[r] = e["when"]
    order = {r["uuid"]: i for i, r in enumerate(rows)}
    return sorted(rows, key=lambda r: (0, -last[r["uuid"]].timestamp()) if r["uuid"] in last else (1, order[r["uuid"]]))


def fingerprint(row: dict, research_task) -> str:
    """What a search row shows that moves when the record changes: the owner (a probate upload makes the
    executor the owner), the status, the list and tag counts, the trace stamp and the one phone the row
    carries, plus the research task the record was judged on."""
    o = row.get("owner") or {}
    ph = row.get("phone") if isinstance(row.get("phone"), dict) else {}
    parts = [o.get("uuid") or "", str(o.get("first_name") or "").lower(), str(o.get("last_name") or "").lower(),
             str(o.get("company") or "").lower(), lanes.status_of(row), row.get("list_count"), row.get("tag_count"),
             bool(row.get("has_phones")), row.get("last_skip_traced") or "", gate.digits(ph.get("number")),
             ph.get("tag_count"), str(ph.get("status") or ""), research_task or 0]
    return hashlib.sha256(json.dumps(parts, default=str).encode("utf-8")).hexdigest()[:16]


def _research_state(workflow: str) -> tuple[dict, set]:
    """(record -> its newest finished research task, records with research still open), in two reads:
    asking once per record would be thousands of small reads to sort a list that has not changed."""
    with store._LOCK, store._db() as c:
        done = {r["record_uuid"]: r["id"] for r in c.execute(
            "SELECT record_uuid, MAX(id) AS id FROM tasks WHERE workflow=? AND status='done' AND record_uuid<>'' "
            "GROUP BY record_uuid", (workflow,)).fetchall()}
        running = {r["record_uuid"] for r in c.execute(
            "SELECT DISTINCT record_uuid FROM tasks WHERE workflow=? AND status IN (%s) AND record_uuid<>''"
            % ",".join("?" * len(OPEN_TASKS)), (workflow,) + OPEN_TASKS).fetchall()}
    return done, running


def _fresh(seen: dict, now: datetime, recheck: timedelta) -> bool:
    try:
        return now - datetime.fromisoformat(str(seen.get("at"))) < recheck
    except (TypeError, ValueError):
        return False


def remember(ctx: Ctx, u: str, fp: str, v: dict, verdict: str) -> None:
    """The verdict, kept in plan mode too (it is the autopilot's own knowledge, and the list for a person
    is built from it). Masked numbers only: the full numbers stay on the record."""
    ledger.set_fact(u, FACT, {"fp": fp, "at": ctx.now.isoformat(timespec="seconds"), "live": bool(ctx.live),
                              "verdict": verdict, "moves": len(v["move"]), "street": v["street"], "field": v["field"],
                              "executor": " and ".join(v["names"]), "why": v["why"], "person": v["person"],
                              "dial_left": v.get("dial_left"),
                              "numbers": ", ".join(mask(p.get("number")) for p in v["unsure"])})


def person_file() -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "phonecleanup"
    d.mkdir(parents=True, exist_ok=True)
    return d / "for_a_person.csv"


def write_person_file(in_scope: set) -> int:
    """Every record in scope that waits on a person, rebuilt from the remembered verdicts so a record that
    was sorted out since leaves the list."""
    rows = []
    for u, f in researchqueue.facts_by_key(FACT).items():
        if u in in_scope and isinstance(f, dict) and f.get("person"):
            rows.append({"record": u, "street": f.get("street") or "", "executor": f.get("executor") or "",
                         "field": f.get("field") or "", "why": f.get("person") or "", "numbers": f.get("numbers") or ""})
    rows.sort(key=lambda r: (r["street"], r["record"]))
    with person_file().open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FILE_COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


# ---------------------------------------------------------------------- the stage
def run_phonecleanup(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    words = vocab(o)
    try:
        activity.sync(ctx.crm, "state", now=ctx.now, max_calls=int(o["max_log_calls"]))
    except Exception as exc:  # noqa: BLE001  (the order is a nicety; the work is not)
        ctx.out.problem("could not read the change log (%s); records were read in search order" % str(exc)[:80])
    rows = _scope_rows(ctx, o)
    ctx.out.found = len(rows)
    if not rows:
        ctx.out.head = "No record is on the scope lists or carries a scope tag, so there is nothing to sort."
        return
    rows = newest_first(rows, ctx.now)
    done_tasks, running = _research_state(str(o["research_workflow"]))
    memory = researchqueue.facts_by_key(FACT)
    recheck = timedelta(days=float(o["recheck_days"]))
    max_reads, max_records = int(o["max_reads"]), int(o["max_records"])
    reads = 0
    acted: list[dict] = []
    kept: Counter = Counter()
    state = {"checked": False, "broken": False}
    for row in rows:
        u = row["uuid"]
        if state["broken"]:
            ctx.out.tally["not looked at: the run stopped after a phone change that did not take"] += 1
            continue
        if lanes.status_of(row) in lanes.LEAD_STATUSES:
            ctx.out.tally["a lead: the lead manager has it"] += 1
            continue
        if _entity(row.get("owner") or {}):
            ctx.out.tally[ENTITY_WHY] += 1
            continue
        if u in running:
            ctx.out.tally["research is still running on it: looked at on a later run"] += 1
            continue
        fp = fingerprint(row, done_tasks.get(u))
        seen = memory.get(u) if isinstance(memory.get(u), dict) else None
        fresh = bool(seen) and seen.get("fp") == fp and _fresh(seen, ctx.now, recheck)
        # A plan run's "would move" is not work done: a live run reads that record again and does it.
        if fresh and not (ctx.live and seen.get("verdict") == "move" and not seen.get("live")):
            if seen.get("verdict") == "move" and not ctx.live:
                if len(acted) >= max_records:
                    ctx.out.tally["left for the next run (change cap %d)" % max_records] += 1
                    continue
                acted.append({"street": seen.get("street") or addr(row), "n": int(seen.get("moves") or 0),
                              "field": seen.get("field") or "", "dial_left": seen.get("dial_left")})
                continue
            ctx.out.tally["looked at before, and nothing has changed since"] += 1
            continue
        if len(acted) >= max_records:
            ctx.out.tally["left for the next run (change cap %d)" % max_records] += 1
            continue
        if reads >= max_reads:
            ctx.out.tally["left for the next run (read cap %d)" % max_reads] += 1
            continue
        reads += 1
        try:
            rec = ctx.crm.record(u)
        except Exception as exc:  # noqa: BLE001  (one unreadable record must not stop the rest)
            ctx.out.problem("could not read a record (%s); it is looked at again next run" % str(exc)[:80])
            continue
        v = judge(ctx, rec, row, o, words)
        for k, n in v["kept"].items():
            kept[k] += n
        if v["verdict"] == "retry":
            ctx.out.tally[v["why"]] += 1       # not remembered: the record is read again next run
            continue
        if v["verdict"] != "move":
            if v["why"]:
                ctx.out.tally[v["why"]] += 1
            remember(ctx, u, fp, v, v["verdict"])
            continue
        if not write_moves(ctx, rec, v["move"], o, state):
            continue                         # nothing remembered: the record is looked at again next run
        acted.append({"street": v["street"], "n": len(v["move"]), "field": v["field"], "dial_left": v["dial_left"]})
        if ctx.live:
            ledger.event(ctx.run_id, st.id, u, KIND, {"moved": len(v["move"]), "field": v["field"],
                                                      "left": len(v["unsure"])}, now=ctx.now, tz=ctx.policy.tz)
        remember(ctx, u, fp, v, "moved" if ctx.live else "move")
    waiting = write_person_file({r["uuid"] for r in rows})
    _summarize(ctx, o, acted, kept, reads, len(rows), waiting)


def _summarize(ctx: Ctx, o: dict, acted: list[dict], kept: Counter, reads: int, scope: int, waiting: int) -> None:
    lower = o["lower_tier"]
    n = sum(a["n"] for a in acted)
    ctx.out.acted = len(acted)
    wait = ("; %d record(s) wait on a person" % waiting) if waiting else ""
    if acted:
        ctx.out.head = "%s %d relative's number(s) down to %s on %d record(s) where an executor or decision maker is named%s." % (
            "Moved" if ctx.live else "Would move", n, lower, len(acted), wait)
    else:
        ctx.out.head = "No relative's number to move down today (%d record(s) read this run)%s." % (reads, wait)
    for a in acted[:8]:
        ctx.out.line("%s: %d number(s) %s down to %s (executor from %s)" % (
            a["street"], a["n"], "moved" if ctx.live else "to move", lower, a["field"] or "the record"))
    bare = sum(1 for a in acted if a.get("dial_left") == 0)
    if bare:
        ctx.out.line("%d of these record(s) %s no Dial First or Second number left, so the executor still needs a skip trace."
                     % (bare, "have" if ctx.live else "would have"))
    if kept:
        ctx.out.line("Numbers that stayed where they were: %s." % ", ".join("%d %s" % (c, k) for k, c in kept.most_common(9)))
    ctx.out.line("Read %d of %d record(s) on the scope lists and tags, newest first." % (reads, scope))
    if waiting:
        ctx.out.problem("Some records need a person before their numbers can be sorted: %d record(s), listed in %s"
                        % (waiting, person_file()))
