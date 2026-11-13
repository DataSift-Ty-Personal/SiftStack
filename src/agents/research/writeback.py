"""What a deep prospect may write onto a DataSift record, as small rules the chain calls.

The write-back used to be one long function in dream100_dp: whatever the analysis file said went
onto the record. Each rule below answers something that went wrong with that, or would have.

  * Every Dial Third number went on the record beside the First and Second ones, although the team
    dials only the top two tiers (decision D12).
  * A signer named in an obituary nobody opened, with no number of their own, could be written as
    the decision maker, and the next caller asked for a person nobody had verified.
  * The owner's deceased mark was set on whoever the record's owner was. On a probate record that
    is the personal representative the court named, who is alive.
  * A litigator's number was skipped as a phone while the record stayed in every lane.
  * Numbers landed through the phone route, which DataSift does not count as a skip trace, so the
    record kept reading "never skip traced" and fell out of every Ready to Call preset.
  * The research level lived only in the PDF, and the PDF lived only on the box.

Two kinds of rule, on purpose:

  REMOVES a write. On by default, because it can only make a record safer.
      AGENTS_DP_THIRD_TIER      reserve   First and Second are written; Third only when the record
                                          has nothing better. Also: person, never, always.
      AGENTS_DP_VERIFIED_ONLY   1         The decision maker fields name the owner of record or a
                                          verified person, nobody else; and the owner's deceased
                                          mark and the Owner Deceased field are held back when the
                                          owner on the record is a person the research shows living.
  ADDS a write this chain has never made on a real record. Off until one record proves it.
      AGENTS_DP_LEVEL           0         the research level, 1 to 4, in a custom field
      AGENTS_DP_LEVEL_FIELD     Research Level
      AGENTS_DP_HEIR_TAG        0         the tag Heir Identified
      AGENTS_DP_SKIPTRACE_COUNTER 0       the owner's skip trace counter, when numbers land
      AGENTS_DP_LITIGATOR       0         Litigator and Do Not Market on the record and on the
                                          owner's other records, with a note
      AGENTS_DP_DOCUMENT        0         the report PDF in the record's documents
      AGENTS_DP_OWNER_SWAP      0         the owner changed to a verified person at their own address
  What research reads first (plan 3.3):
      AGENTS_DP_ORDER           trace_first   today's order. free_first reads the free facts and
                                              researches before any paid trace.
      AGENTS_DEED_DIR           <data dir>/deeds   where a saved deed lookup is read from

A person is VERIFIED when both hold: an obituary or court record opened in this run names them,
and a number matched their name (Trestle Real Contact). Nothing in this module decides that an
owner died. That is still researcher.enforce_analysis, and every rule here that depends on a death
reads that one answer and may only ask for more on top of it.

Standard library only, and nothing is read from the network at import: dream100_dp imports this on
a workstation that may not carry the model clients.
"""
from __future__ import annotations

import os
import re
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

TOP_TIERS = ("Dial First", "Dial Second")
RESERVE_TIER = "Dial Third"
BAD_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
SIGNER_ROLES = ("signer", "decision maker")
HEIR_TAG = "Heir Identified"
LITIGATOR_TAG = "Litigator"
DO_NOT_MARKET_TAG = "Do Not Market"
LEVEL_FIELD = "Research Level"
OWNER_DECEASED_FIELD = "Owner Deceased"
DM_FIELDS = ("Decision Maker", "DM Relationship")
LITIGATOR_NOTE = ("A number on this owner belongs to a known TCPA litigator. Do not call, text or mail, on any "
                  "record this owner holds.")
THIRD_MODES = ("reserve", "person", "never", "always")
ORDERS = ("trace_first", "free_first")
SETTING_NAMES = ("AGENTS_DP_THIRD_TIER", "AGENTS_DP_VERIFIED_ONLY", "AGENTS_DP_LEVEL", "AGENTS_DP_LEVEL_FIELD",
                 "AGENTS_DP_HEIR_TAG", "AGENTS_DP_SKIPTRACE_COUNTER", "AGENTS_DP_LITIGATOR", "AGENTS_DP_DOCUMENT",
                 "AGENTS_DP_OWNER_SWAP", "AGENTS_DP_ORDER", "AGENTS_DEED_DIR")

# A signer line that names a role or a group, not a person (the same test the signer guard uses).
STRUCTURAL = re.compile(r"\b(trust|trustee|estate|heirs?|beneficiar\w*|successor|if any|unknown|guardian|"
                        r"personal representative|executor of|administrator of)\b", re.I)
RULED_OUT = re.compile(r"ruled out|not this owner|not the owner|different person|unrelated|stranger|wrong person", re.I)
COURT = re.compile(r"\b(probate|chancery|court|letters testamentary|letters of administration|"
                   r"personal representative|executor|executrix|administrator|administratrix|"
                   r"notice to creditors|docket|case no)\b", re.I)
DEAD_MARK = re.compile(r"preceded\s+(?:her|him|them)?\s*in death|predeceased|preceded by", re.I)
LIVE_MARK = re.compile(r"surviv|leaves behind|leaves to cherish|left to cherish", re.I)
_YES, _NO = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


# ---------------------------------------------------------------- settings --

def _switch(value, default: bool) -> bool:
    """A value nobody can read keeps the default, and every default is the safe side."""
    s = str(value if value is not None else "").strip().lower()
    return True if s in _YES else False if s in _NO else default


def settings(env=None) -> dict:
    """Every setting of the research write-back, read from the environment when it is asked for
    (a batch child process and a Fly secret both arrive that way)."""
    e = os.environ if env is None else env
    third = str(e.get("AGENTS_DP_THIRD_TIER") or "reserve").strip().lower()
    order = str(e.get("AGENTS_DP_ORDER") or "trace_first").strip().lower()
    data_dir = e.get("AGENTS_DATA_DIR") or str(ROOT / "output" / "agents")
    return {"third_tier": third if third in THIRD_MODES else "reserve",
            "verified_only": _switch(e.get("AGENTS_DP_VERIFIED_ONLY"), True),
            "level": _switch(e.get("AGENTS_DP_LEVEL"), False),
            "level_field": str(e.get("AGENTS_DP_LEVEL_FIELD") or LEVEL_FIELD).strip() or LEVEL_FIELD,
            "heir_tag": _switch(e.get("AGENTS_DP_HEIR_TAG"), False),
            "skiptrace_counter": _switch(e.get("AGENTS_DP_SKIPTRACE_COUNTER"), False),
            "litigator": _switch(e.get("AGENTS_DP_LITIGATOR"), False),
            "document": _switch(e.get("AGENTS_DP_DOCUMENT"), False),
            "owner_swap": _switch(e.get("AGENTS_DP_OWNER_SWAP"), False),
            "order": order if order in ORDERS else "trace_first",
            "deed_dir": str(e.get("AGENTS_DEED_DIR") or Path(data_dir) / "deeds")}


# ------------------------------------------------------------------- names --
# The same test open_research.same_person applies, kept here so these rules import nothing but the
# standard library. tests/test_writeback.py holds the two side by side on one table of names.

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr", "estate", "of", "the", "and"}
_GENERATION = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}


def name_tokens(name: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split() if len(t) > 1 and t not in _SUFFIXES]


def _generation(name: str) -> str:
    for t in re.sub(r"[^a-z ]", " ", (name or "").lower()).split():
        if t in _GENERATION:
            return _GENERATION[t]
    return ""


def _parts(name: str) -> tuple[str, str, set]:
    s = re.sub(r"\(.*?\)", " ", name or "")
    if "," in s:
        last, rest = s.split(",", 1)
        toks = name_tokens(rest) + name_tokens(last)
    else:
        toks = name_tokens(s)
    return (toks[0], toks[-1], set(toks)) if toks else ("", "", set())


def same_name(a: str, b: str, strict: bool = False) -> bool:
    """Two spellings of one person. A middle initial or county order does not split a person in
    two, but Sr. and Jr. are a father and a son, and a shared middle name and surname is a parent
    and a child. `strict` is for accusations (this dead person is that signer)."""
    ga, gb = _generation(a), _generation(b)
    if ga != gb and ((ga and gb) or strict):
        return False
    fa, la, sa = _parts(a)
    fb, lb, sb = _parts(b)
    if not fa or not fb:
        return False
    if len(sa) == 1 or len(sb) == 1:
        return sa == sb
    if fa == lb and la == fb:
        return True
    first_ok = fa == fb or (not strict and (fa in sb - {lb} or fb in sa - {la}))
    last_ok = la == lb or la in sb or lb in sa
    return first_ok and last_ok


def plain(name) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", " ", str(name or ""))).strip()


def digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[1:] if len(d) == 11 and d.startswith("1") else d


def _titles(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("title") or t.get("name")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def _words(text: str) -> set:
    return set(re.sub(r"[^a-z ]", " ", (text or "").lower()).split())


def _names(text: str, name: str) -> bool:
    """Does the text carry this person's first and last name as whole words?"""
    first, last, toks = _parts(name)
    w = _words(text)
    return len(toks) >= 2 and first in w and last in w


def norm_url(u: str) -> str:
    return re.sub(r"[#?].*$", "", (u or "").strip().lower()).rstrip("/").replace("http://", "https://")


def owner_label(owner: dict) -> str:
    owner = owner or {}
    return (" ".join(str(owner.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
            or str(owner.get("company") or "").strip())


_PARTICLES = {"de", "del", "della", "der", "di", "da", "la", "le", "van", "von", "st", "ste", "bin", "al"}


def _looks_like_name(s: str) -> bool:
    """Two to five words that each read like part of a name. "Pell Quillfeather" is one; "if a
    probate is opened", the tail of a signer line about a role, is not."""
    words = plain(s).split()
    if not 2 <= len(words) <= 5:
        return False
    for w in words:
        low = w.strip(".'").lower()
        if low in _PARTICLES or low in _GENERATION:
            continue
        if not re.fullmatch(r"[A-Z][A-Za-z.'-]*", w):
            return False
    return True


def signer_names(an: dict) -> list[str]:
    """The PEOPLE this file says must sign, each once. A line about a role or a group ("the
    executor of the estate, if a probate is opened") names nobody and is not counted."""
    out: list[str] = []

    def add(n, strict):
        n = plain(n)
        if not name_tokens(n) or STRUCTURAL.search(n) or (strict and not _looks_like_name(n)):
            return
        if not any(same_name(n, x) for x in out):
            out.append(n)

    for m in (an or {}).get("must_sign") or []:
        line = re.sub(r"\(.*?\)", " ", str((m.get("who") if isinstance(m, dict) else m) or ""))
        for part in re.split(r",|;|&|\band\b", line):
            add(part, True)
    for p in (an or {}).get("people") or []:
        if p.get("role") in SIGNER_ROLES and p.get("living") != "Deceased":
            add(p.get("name") or "", False)
    return out


def is_decedent(name: str, an: dict) -> bool:
    """Is this the person whose death the existing guard confirmed? The guard's own test (two name
    words in common), plus one thing it does not ask: a Sr. is never his Jr."""
    death = (an or {}).get("death") or {}
    if not death.get("owner_deceased"):
        return False
    tok = lambda s: {t for t in re.sub(r"[^a-z ]", " ", (s or "").lower()).split() if len(t) > 1}  # noqa: E731
    ga, gb = _generation(name), _generation(death.get("decedent") or "")
    return len(tok(name) & tok(death.get("decedent"))) >= 2 and not (ga and gb and ga != gb)


def deceased_flag(an: dict, owner: dict, strict: bool = True) -> tuple[bool, str]:
    """May the record's OWNER be marked deceased? The mark used to go on whoever held the owner
    slot once the guard confirmed the death on the title. On a probate record that is the personal
    representative the court named; where a husband and wife both hold title and she died, it is
    him; after an owner change it is the heir. Marking any of them dead takes a living person out
    of every lane.

    So the mark is held back in exactly the cases where the research itself shows it is wrong: the
    owner on the record is a person the analysis calls Living and whose name is not also the dead
    person's, or one is a senior and the other a junior. Everywhere else the rule is what it was,
    because the opposite mistake is the worse one: an owner stored as "D Quillfeather" who really
    did die must still be marked. `strict` False is the rule before 2026-11-13."""
    death = (an or {}).get("death") or {}
    if not death.get("owner_deceased"):
        return False, ""
    if not strict:
        return True, ""
    label, dead = owner_label(owner), plain(death.get("decedent"))
    ga, gb = _generation(label), _generation(dead)
    if ga and gb and ga != gb:
        return False, ("the record's owner (%s) is not the person who died (%s): one is a senior and the other a junior, "
                       "so the owner's deceased mark was left alone" % (label, dead))
    living = next((plain(p.get("name")) for p in (an or {}).get("people") or []
                   if p.get("living") == "Living" and label and same_name(plain(p.get("name")), label, strict=True)), "")
    if living and not same_name(label, dead, strict=True):
        return False, ("the record's owner (%s) is living in the research and is not the person who died (%s), "
                       "so the owner's deceased mark was left alone" % (label, dead or "not named"))
    return True, ""


# --------------------------------------------------------- which numbers go on --

def _has_top(dial: list, crm_phones: dict | None = None) -> bool:
    for r in dial or []:
        if r.get("on_crm") and not r.get("do_not_dial") and r.get("tier") in TOP_TIERS:
            return True
    for c in (crm_phones or {}).values():
        if str(c.get("status") or "").upper() not in BAD_STATUSES and set(c.get("tags") or []) & set(TOP_TIERS):
            return True
    return False


def tier_rule(dial: list, items: list, mode: str = "reserve", crm_phones: dict | None = None) -> tuple[list, list]:
    """Which planned NEW numbers go on the record (decision D12, plan 1.14 and 3.4).

    Dial First and Second are written. Dial Third is a reserve: it goes on only when the record
    would otherwise hold no First or Second number at all, so a record is never left with nothing
    to dial. Anything lower, and anything unscored, is never written, whatever the mode. Before
    this rule every Third number was written, and a caller working the record's phone list dialed
    numbers the team's own rule says not to dial.

    `mode`: reserve (the rule above), person (Third is also kept for a person who has no First or
    Second number of their own), never, always (First to Third, the rule before 2026-11-13).
    Returns (kept, held); each held entry says why."""
    by = {r.get("number"): r for r in dial or []}

    def tier_of(it):
        r = by.get(it.get("number")) or {}
        return r.get("tier") or next((t for t in it.get("tags") or [] if t in TOP_TIERS or t == RESERVE_TIER), "")

    top = [it for it in items or [] if tier_of(it) in TOP_TIERS]
    third = [it for it in items or [] if tier_of(it) == RESERVE_TIER]
    held = [{"number": it.get("number"), "tier": tier_of(it) or "unscored",
             "why": "below Dial Third or unscored: never written"}
            for it in items or [] if it not in top and it not in third]
    if mode == "always":
        return top + third, held
    if mode == "never":
        return top, held + [{"number": it["number"], "tier": RESERVE_TIER, "why": "Dial Third is not written"}
                            for it in third]
    if not (top or _has_top(dial, crm_phones)):
        return third, held                      # nothing better anywhere on the record: the reserve is used
    keep3 = []
    if mode == "person":
        covered = set()                         # people who already have a First or Second number
        for r in dial or []:
            if r.get("tier") in TOP_TIERS and not r.get("do_not_dial"):
                covered |= {z.get("name") for z in r.get("reaches") or []}
        keep3 = [it for it in third
                 if any(z.get("name") not in covered for z in (by.get(it["number"]) or {}).get("reaches") or [])]
    why = ("Dial Third held in reserve: everyone it reaches has a better number" if mode == "person"
           else "Dial Third held in reserve: the record has a First or Second number")
    return top + keep3, held + [{"number": it["number"], "tier": RESERVE_TIER, "why": why}
                                for it in third if it not in keep3]


def dial_first(dial: list, mode: str = "reserve") -> list:
    """The dialable rows a call order may name: First and Second, Third only as the reserve."""
    ok = [r for r in dial or [] if not r.get("do_not_dial")]
    top = [r for r in ok if r.get("tier") in TOP_TIERS]
    third = [r for r in ok if r.get("tier") == RESERVE_TIER]
    if mode == "always":
        return [r for r in ok if r.get("tier") in TOP_TIERS or r.get("tier") == RESERVE_TIER]
    if mode == "never":
        return top
    return top or third


# --------------------------------------------------------------- litigators --

def litigator_numbers(dial: list, people: list, scores: dict, verify: dict | None = None) -> list[str]:
    """Every number tied to this record that a check flagged as a litigator's: on the dial sheet or
    on anyone in the record's people list, by the tier lookup or by the name check."""
    nums = {r.get("number") for r in dial or []} | {q.get("number") for p in people or [] for q in p.get("phones") or []}
    nums.discard(None)
    flagged = {r.get("number") for r in dial or [] if r.get("litigator")}
    flagged |= {n for n in nums if ((scores or {}).get(n) or {}).get("litigator")}
    flagged |= {digits(v.get("number")) for v in (verify or {}).values() if isinstance(v, dict) and v.get("litigator")}
    return sorted(n for n in flagged if n in nums)


# ----------------------------------------------------------------- verified --

def named_among_living(text: str, name: str) -> bool:
    """Does this text name the person, and not only in its list of the dead? An obituary names the
    people who died first in the same paragraph as the survivors, so a first name alone proves
    nothing: the nearest marker before the mention decides which list it sits in."""
    first, last, toks = _parts(name)
    if len(toks) < 2:
        return False                       # one word is not a name anyone can be held to
    low = (text or "").lower()
    if last not in _words(low):
        return False
    for m in re.finditer(r"\b%s\b" % re.escape(first), low):
        back = low[max(0, m.start() - 1200):m.start()]      # long enough to hold a whole list of the dead
        dead = [x.end() for x in DEAD_MARK.finditer(back)]
        live = [x.end() for x in LIVE_MARK.finditer(back)]
        if dead and (not live or dead[-1] > live[-1]):
            continue
        return True
    return False


def evidence(research: dict, an: dict, urls=None, webcache=None) -> list[dict]:
    """The obituary and court pages this run can stand on, each with the text that was read.

    Where the low-cost engine ran, its web cache holds every page it OPENED with the page's own
    text, so a page that only turned up in a search result is not evidence and the text checked is
    the page itself, not the model's account of it. Where the Claude researcher ran there is no
    page cache: the page must be in the run's visited list, and the text is the verbatim quote the
    research kept, the same trust today's death guard gives it."""
    research, an = research or {}, an or {}
    pages = None
    if isinstance(webcache, dict):
        pages = {norm_url(u): v for u, v in (webcache.get("page") or {}).items() if isinstance(v, dict)}
    visited = {norm_url(u) for u in urls or []}
    out, seen = [], set()

    def add(url, kind, of, quote):
        key = norm_url(url)
        if not key or (kind, key) in seen:
            return
        if pages is not None:
            hit = pages.get(key) or {}
            text = hit.get("text") or ""
            if not (hit.get("ok") and text):
                return                      # found by a search, never opened
            if kind == "obituary" and of and not _names(text, of):
                return                      # an opened page that is not this person's obituary
            how = "page opened this run"
        else:
            text = (quote or "").strip()
            if key not in visited or not text:
                return
            how = "quoted from a page visited this run"
        seen.add((kind, key))
        out.append({"url": url, "kind": kind, "of": of, "text": text, "how": how})

    for dec in research.get("decedents") or []:
        if not RULED_OUT.search(dec.get("name") or ""):
            add(dec.get("obituary_url"), "obituary", dec.get("name") or "", dec.get("survivors_verbatim"))
    death = an.get("death") or {}
    if death.get("obituary_url"):
        quotes = [q.get("text") or "" for q in an.get("survivors_quotes") or [] if isinstance(q, dict)]
        quotes += [x.get("survivors_verbatim") or "" for x in research.get("decedents") or []
                   if same_name(x.get("name") or "", death.get("decedent") or "")]
        add(death["obituary_url"], "obituary", death.get("decedent") or "", "\n".join(q for q in quotes if q))
    for key in ("owner_status_evidence", "identity_proof", "title_and_sales", "life_stage_signals", "property_history"):
        for e in research.get(key) or []:
            if COURT.search(" ".join(str(e.get(k) or "") for k in ("claim", "quote", "url"))):
                add(e.get("url"), "court", "", e.get("quote"))
    for p in research.get("people") or []:
        if p.get("source_url") and COURT.search(" ".join(str(p.get(k) or "") for k in ("quote", "source_url"))):
            add(p.get("source_url"), "court", "", p.get("quote"))
    return out


def number_matches(names: list, dial: list, verify: dict | None) -> list[str]:
    """Numbers the name check tied to one of these names, and that may still be dialed. A CRM
    status of CORRECT is not used: on one record the number marked correct belonged to the
    representative's spouse."""
    want = {plain(n).lower() for n in names if plain(n)}
    by = {r.get("number"): r for r in dial or []}
    out = set()
    for v in (verify or {}).values():
        if not isinstance(v, dict) or not v.get("match") or v.get("litigator"):
            continue
        if plain(v.get("name")).lower() not in want:
            continue
        r = by.get(digits(v.get("number")))
        if r and not r.get("do_not_dial"):
            out.add(r["number"])
    return sorted(out)


def verified_people(an: dict, research: dict, dial: list, verify: dict | None, urls=None, webcache=None) -> dict:
    """name -> what is known about each signer the analysis names (plan 3.4).

    VERIFIED means both: an obituary or court record opened in this run names the person among the
    living, and a number matched their name. Either one alone is a lead. A person the analysis
    does not call Living is never verified, so an unverified signer can never read like a
    confirmed one."""
    ev = evidence(research, an, urls, webcache)
    out = {}
    for p in (an or {}).get("people") or []:
        name = plain(p.get("name"))
        if not name or p.get("role") not in SIGNER_ROLES:
            continue
        row = {"name": name, "documented": False, "source": "", "kind": "", "number": "", "verified": False, "why": ""}
        out[name] = row
        if p.get("living") != "Living":
            row["why"] = "not shown living (%s)" % (p.get("living") or "no status")
            continue
        legal = [n for n in dict.fromkeys([name, plain(p.get("rc_name"))]) if n]
        for e in ev:
            if any(named_among_living(e["text"], n) for n in legal):
                row.update(documented=True, source=e["url"], kind=e["kind"])
                break
        nums = number_matches(legal, dial, verify)
        row["number"] = nums[0] if nums else ""
        row["verified"] = row["documented"] and bool(nums)
        if row["verified"]:
            row["why"] = ("named in %s record opened this run, and a number matches their name"
                          % ("a court" if row["kind"] == "court" else "an obituary"))
        else:
            row["why"] = "; ".join(([] if row["documented"] else ["no obituary or court record opened this run names them"])
                                   + ([] if nums else ["no number matches their name"]))
    return out


def heir_identified(an: dict, verified: dict) -> tuple[bool, str]:
    """May the record carry the tag Heir Identified? The tag opens a dead owner's record to the
    heir lane and takes it out of every cold preset, so it needs the death confirmed by the
    existing guard AND a verified signer, never the research's say-so alone."""
    if not ((an or {}).get("death") or {}).get("owner_deceased"):
        return False, "the owner's death is not confirmed"
    good = [v["name"] for v in (verified or {}).values() if v.get("verified")]
    if not good:
        return False, "no signer is verified yet"
    return True, "verified: " + ", ".join(good[:3])


def research_level(an: dict) -> tuple[int, str]:
    """The deep prospecting level, 1 to 4, from facts the guards already settled. A deep prospect
    starts at 2; 3 needs the owner's death confirmed; 4 is a dead owner with more than one signer,
    or a title the research itself calls level 4. The model's own level never raises a record to 3
    without a confirmed death."""
    said = re.search(r"\b(?:level|l)\s*([1-4])\b", str((an or {}).get("level") or ""), re.I)
    said = int(said.group(1)) if said else 0
    dead = bool(((an or {}).get("death") or {}).get("owner_deceased"))
    n = len(signer_names(an))
    if dead and n >= 2:
        return 4, "the owner has died and %d people must sign" % n
    if said == 4:
        return 4, "the research calls the title complex"
    if dead:
        return 3, "the owner has died"
    return 2, "the owner is not confirmed deceased"


# "the estate of Dorn Quillfeather", "personal representative of the estate of ...": a role and the
# name it is the role OF. Only the role words ignore case; the name must start with capitals, or
# the pattern would swallow the rest of the sentence.
_ROLE_OF = re.compile(r"\b(?i:estate|heirs?|devisees?|beneficiaries|trustees?|personal representative|executor|executrix|"
                      r"administrator|administratrix)\s+(?i:of)\s+(?i:the\s+)?(?i:late\s+)?(?i:estate\s+of\s+)?(?i:the\s+)?"
                      r"(?:[A-Z][\w.'-]*\s*){1,5}")


def named_in(text: str, name: str) -> bool:
    """Is this person named in the text, first name beside last? Looking for both words anywhere in
    it is not enough: "Dorn Quillfeather and Marla Brackwater" holds the words of a third person,
    Marla Quillfeather, who is not in it. Read off a live record, where that held a field back."""
    first, last, toks = _parts(name)
    if len(toks) < 2:
        return False
    low = re.sub(r"\(.*?\)", " ", text or "").lower()
    f, s = re.escape(first), re.escape(last)
    return bool(re.search(r"\b%s\b(?:[\s.]+[a-z'.-]+){0,2}[\s.]+%s\b" % (f, s), low)             # First [Middle] Last
                or re.search(r"\b%s\b\s*(?:,|&|\band\b)\s*(?:[a-z'.-]+\s+){1,3}?%s\b" % (f, s), low)   # First and Other Last
                or re.search(r"\b%s\b,?\s+%s\b" % (s, f), low))                                    # Last, First


def filter_fields(fields: dict, an: dict, verified: dict, owner_names: list) -> tuple[dict, list]:
    """Drop the decision maker fields when they name a person the rule can show does not belong
    there: someone who has died, or someone in the research file who is neither an owner of record
    nor verified. Before this, a signer nobody had verified went into the Decision Maker field and
    read on the record exactly like a confirmed one.

    It removes only on that positive showing. A field that names the owners of record (living or
    not yet confirmed), a role ("Successor trustee of the trust"), or a name the research file does
    not hold at all passes as written, because a rule that guesses would take good fields with it.
    Every other field passes untouched."""
    fields = dict(fields or {})
    value = str(fields.get(DM_FIELDS[0]) or "").strip()
    if not value:
        return fields, []
    death = (an or {}).get("death") or {}
    dead = bool(death.get("owner_deceased"))
    decedent = plain(death.get("decedent"))
    said = _ROLE_OF.sub(" ", value)             # "the estate of X" names a role, not X
    why = ""
    if dead and decedent and named_in(said, decedent) and _generation(said) in ("", _generation(decedent)):
        why = "%s has died" % decedent
    for p in ([] if why else (an or {}).get("people") or []):
        nm = plain(p.get("name"))
        if not named_in(said, nm):
            continue
        # Once somebody on the title has died the name test is strict in both directions: a son
        # named for his father is neither his dead father nor the owner of record. With nobody
        # dead there is no one to confuse him with, and "Jr" missing from the CRM's copy of the
        # name must not cost a living owner his own decision maker field.
        if p.get("living") == "Deceased" or (dead and same_name(nm, decedent, strict=True)):
            why = "%s has died" % nm
        elif p.get("role") == "owner" or any(same_name(nm, o, strict=dead) for o in owner_names or []):
            continue                        # an owner of record: nothing about who owns it is being changed
        elif not ((verified or {}).get(nm) or {}).get("verified"):
            why = "%s is not verified (%s)" % (nm, ((verified or {}).get(nm) or {}).get("why")
                                               or "not a signer in the research file")
        if why:
            break
    if not why:
        return fields, []
    gone = " and ".join(k for k in DM_FIELDS if k in fields)
    return {k: v for k, v in fields.items() if k not in DM_FIELDS}, ["%s not written: %s" % (gone, why)]


def swap_plan(d: dict, verified: dict, owner: dict | None = None) -> tuple[dict | None, list]:
    """The owner change the record may take, or the reasons it may not (plan 3.4).

    The owner is changed only when the record's owner is the person whose death the guard
    confirmed, only to a VERIFIED person, and only at that person's own complete mailing address.
    The property address is never touched. `owner` is the owner as the record holds it right now
    (the records file can be hours old). Returns (plan, refusals)."""
    an, x = d.get("an") or {}, d.get("x") or {}
    ow = owner if owner is not None else ((x.get("record") or {}).get("owner") or {})
    if not (an.get("death") or {}).get("owner_deceased"):
        return None, ["the owner's death is not confirmed"]
    if ow.get("company") and not ow.get("first_name"):
        return None, ["the owner is a company or a trust"]
    if not is_decedent(owner_label(ow), an):
        return None, ["the record's owner (%s) is not the person who died" % (owner_label(ow) or "no name")]
    people = {plain(p.get("name")): p for p in an.get("people") or [] if plain(p.get("name"))}
    order = [plain(c.get("who")) for c in an.get("call_order") or []] + signer_names(an) + list(people)
    pick = ""
    for cand in order:
        nm = next((n for n in people if same_name(n, cand)), "")
        if nm and ((verified or {}).get(nm) or {}).get("verified"):
            pick = nm
            break
    if not pick:
        return None, ["no verified person to change the owner to"]
    ap = people[pick]
    sp = (d.get("pidx") or {}).get(ap.get("ss_key") or "") or {}
    if sp.get("own_trace_deceased_flag") or sp.get("ss_deceased"):
        return None, ["the skip trace flags %s as deceased; a person checks before the owner is changed" % pick]
    # Their OWN address: one their own skip trace matched them at (it returned a number for them
    # as the subject). An address that only rode in on the dead owner's relative list can be years
    # old, and is very often the dead owner's own house.
    if not any(q.get("own_trace") for q in sp.get("phones") or []):
        return None, ["%s has no address their own skip trace confirmed, so there is nowhere sure to send their mail" % pick]
    ta = ap.get("trace_address") or {}
    src = ta if str(ta.get("street") or "").strip() else sp      # one source, never a street from one and a zip from another
    addr = {"street": str(src.get("street") or "").strip(), "city": str(src.get("city") or "").strip(),
            "state": str(src.get("state") or "").strip().upper(), "postal_code": str(src.get("zip") or "").strip()[:5]}
    if not (addr["street"] and addr["city"] and len(addr["state"]) == 2 and re.fullmatch(r"\d{5}", addr["postal_code"])):
        return None, ["%s has no complete mailing address of their own (street, city, state and zip)" % pick]
    # The name the record will carry is the one the research and the number check stand behind (the
    # legal name where the analysis gives one), never the name a trace was run under: a married
    # daughter is traced under her maiden-name record, and that is not who the mail should go to.
    toks = [t for t in (plain(ap.get("rc_name")) or pick).split() if re.sub(r"[^a-z]", "", t.lower()) not in _GENERATION]
    first, last = (toks[0], toks[-1]) if len(toks) >= 2 else ("", "")
    if not (first and last):
        return None, ["%s has no first and last name to write" % pick]
    v = verified[pick]
    rel = (ap.get("tag") or re.split(r"[(,.;]", ap.get("relationship") or "")[0]).strip().lower()
    note = ("Owner changed by research. Was: %s, who has died. Now: %s%s, verified by %s record opened in the research "
            "and a number in their name. The mailing address is their own: %s, %s, %s %s. The property address did not change."
            % (owner_label(ow) or "no name", pick, " (%s)" % rel if rel else "",
               "a court" if v.get("kind") == "court" else "an obituary",
               addr["street"], addr["city"], addr["state"], addr["postal_code"]))
    before = {"uuid": ow.get("uuid"), "first_name": ow.get("first_name"), "last_name": ow.get("last_name"),
              "company": ow.get("company"), "deceased": ow.get("deceased"),
              "address": {k: (ow.get("address") or {}).get(k) for k in ("street", "city", "state", "postal_code")}}
    return {"person": pick, "relationship": rel, "source": v.get("source") or "", "number": v.get("number") or "",
            "to": {"first_name": first, "last_name": last, "address": addr}, "before": before, "note": note}, []


def decide(d: dict, *, verify=None, urls=None, webcache=None, cfg=None) -> dict:
    """Everything the write-back is allowed to do for one record, worked out before any write.
    Pure: it reads the assembled record and the run's own files, never the network."""
    cfg = cfg or settings()
    an, x = d.get("an") or {}, d.get("x") or {}
    research = d.get("research") or {}
    dial = d.get("dial") or []
    owner = (x.get("record") or {}).get("owner") or {}
    owners = [n for n in ((x.get("row") or {}).get("owner"), owner_label(owner), x.get("title_owner"),
                          research.get("owner_of_record")) if n]
    owners += [str(s.get("full_name") or "").strip() for s in (x.get("record") or {}).get("secondary_owners") or []
               if isinstance(s, dict) and str(s.get("full_name") or "").strip()]
    ver = verified_people(an, research, dial, verify, urls, webcache)
    lit = litigator_numbers(dial, d.get("people") or [], d.get("scores") or {}, verify)
    suppress = bool(lit) and cfg["litigator"]
    fields, held = dict(an.get("crm_fields") or {}), []
    if cfg["verified_only"]:
        fields, held = filter_fields(fields, an, ver, owners)
    level = research_level(an)
    if cfg["level"]:
        fields[cfg["level_field"]] = str(level[0])
    heir = heir_identified(an, ver)
    swap, refused = (swap_plan(d, ver) if cfg["owner_swap"] and not suppress else (None, []))
    deceased = deceased_flag(an, owner, strict=cfg["verified_only"] or cfg["owner_swap"])
    fields, held = owner_deceased_field(fields, held, an, deceased)
    return {"verified": ver, "litigators": lit, "suppress": suppress, "fields": fields, "held_fields": held,
            "level": level, "heir": heir, "tags": [HEIR_TAG] if cfg["heir_tag"] and heir[0] and not suppress else [],
            "deceased": deceased, "swap": swap, "swap_refused": refused, "shares": heir_shares(an, research, verified=ver)}


def owner_deceased_field(fields: dict, held: list, an: dict, deceased: tuple) -> tuple[dict, list]:
    """The Owner Deceased field says the same thing as the owner's deceased mark, and the gate
    reads either one as a death. So it follows the mark: where the mark is held back because the
    owner on the record is a living person, the field is not written either. The fields that name
    the person who died (Decedent Name, Date of Death, Obituary URL) still are."""
    if ((an or {}).get("death") or {}).get("owner_deceased") and not deceased[0] and OWNER_DECEASED_FIELD in fields:
        fields = {k: v for k, v in fields.items() if k != OWNER_DECEASED_FIELD}
        held = list(held) + ["%s not written: %s" % (OWNER_DECEASED_FIELD, deceased[1])]
    return fields, held


# ------------------------------------------------------------ skip trace counter --

def trace_key(ledger: dict | None) -> str:
    """Which paid traces this run folder holds. A new order makes a new key."""
    return "|".join(sorted("%s@%s" % (k, (v or {}).get("when") or "") for k, v in (ledger or {}).items()
                           if k.startswith(("smartskip", "tracerfy"))))


def counter_plan(current, landed: bool, prior: dict | None, key: str) -> dict | None:
    """The owner's skip trace counter after numbers land from research (plan 0.10): one more than
    it reads now, written as the TOTAL the route takes. It is counted once per trace: a second
    write-back of the same run finds the same key and leaves the counter alone, or every re-run
    would push the counter one further from the truth."""
    if not landed:
        return None
    if prior and prior.get("ok") and prior.get("key") == key:
        return None
    now = int(current or 0)
    return {"before": now, "total": now + 1, "key": key}


# --------------------------------------------------------------- API helpers --
# Each takes the client the chain already holds (anything with .call(path, method, body)) and uses
# documented routes only. None of them runs unless its setting is on.

def write_counter(api, owner_uuid: str, total: int) -> None:
    api.call("/api/internal/owner/%s/skiptrace-attempts/" % owner_uuid, "POST", {"skiptrace_attempts": int(total)})


def owner_records(api, owner_uuid: str, cap: int = 250) -> list[str]:
    """Every record this owner holds. `owners` is the one filter key the records search accepts
    for it (proven by count on 2026-11-12); the search is a POST read with the GET override."""
    out, off = [], 0
    while len(out) < cap:
        r = api.call("/api/internal/property/", "POST",
                     {"limit": 100, "offset": off, "query": {"must": {"owners": [owner_uuid]}}},
                     headers={"x-http-method-override": "GET"})
        r = r if isinstance(r, dict) else {}
        rows = r.get("results") or r.get("data") or []
        out += [x["uuid"] for x in rows if x.get("uuid")]
        off += 100
        if not rows or off >= int(r.get("count") or 0):
            break
    return out[:cap]


def suppress_litigator(api, uuid: str, owner_uuid: str) -> dict:
    """A litigator's number is a RECORD problem (plan 0.3, decision D22): this record and every
    other record the same owner holds get Litigator and Do Not Market, with one note. Marking only
    the phone left the owner's other numbers, the mail lane and the owner's other houses open.
    A record that already carries both tags is left exactly as it is, so a second run adds nothing."""
    targets, problems = [uuid], []
    if owner_uuid:
        try:
            targets += [u for u in owner_records(api, owner_uuid) if u != uuid]
        except Exception as exc:  # noqa: BLE001  (the one record is still tagged, and the gap is said)
            problems.append("could not list the owner's other records (%s); only this record was tagged" % str(exc)[:100])
    tagged = []
    for u in targets:
        rec = api.call("/api/internal/property/%s/" % u) or {}
        have = {t.lower() for t in _titles(rec.get("tags"))}
        need = [t for t in (LITIGATOR_TAG, DO_NOT_MARKET_TAG) if t.lower() not in have]
        if not need:
            continue
        for t in need:
            api.call("/api/internal/property/%s/add-tags/" % u, "POST", {"tags": t})
        if LITIGATOR_NOTE not in str(rec.get("notes") or ""):
            api.call("/api/internal/property/%s/add-notes/" % u, "POST", {"notes": LITIGATOR_NOTE})
        tagged.append({"uuid": u, "added": need})
    return {"records": len(targets), "tagged": tagged, "problems": problems}


def document_name(slug: str) -> str:
    return "deep-prospect-%s.pdf" % slug


def _post_file(url: str, fields: dict, name: str, data: bytes) -> int:
    import requests                        # late: only a live upload needs it
    r = requests.post(url, data=fields, files={"file": (name, data, "application/pdf")}, timeout=120)
    return r.status_code


def file_document(api, uuid: str, pdf, name: str, *, prop=None, post=None) -> dict:
    """File the report PDF in the record's documents (plan 3.4), through the documented flow: ask
    for an upload target, send the file to it, then register the document on the record.

    One file per record, by name. The same file is never filed twice, and a new report replaces
    the older copy under OUR name only after the new one is registered, so a failure half way
    leaves the old report in place and a caller never finds a stale file beside a current one."""
    data = Path(pdf).read_bytes()
    base = "/api/internal/property/%s/document/" % uuid
    listing = api.call(base + "?limit=100")
    rows = (listing.get("results") or []) if isinstance(listing, dict) else (listing or [])
    mine = [r for r in rows if (r.get("filename") or "") == name]
    if any(int(r.get("size") or -1) == len(data) for r in mine):
        return {"filed": False, "why": "already filed, unchanged", "name": name}
    target = api.call(base + "presigned-url/") or {}
    post_to = target.get("presigned_post") or {}
    if not (post_to.get("url") and target.get("storage_key")):
        raise RuntimeError("the presigned route did not return an upload target")
    status = (post or _post_file)(post_to["url"], post_to.get("fields") or {}, name, data)
    if status not in (200, 201, 204):
        raise RuntimeError("the file store refused the upload (HTTP %s)" % status)
    body = {"filename": name, "extension": "pdf", "storage_key": target["storage_key"], "size": len(data)}
    if isinstance(prop, int) and not isinstance(prop, bool):
        body["prop"] = prop
    doc = api.call(base, "POST", body)
    replaced = []
    for r in mine:
        if r.get("uuid"):
            api.call(base + "%s/" % r["uuid"], "DELETE")
            replaced.append(r["uuid"])
    return {"filed": True, "uuid": (doc.get("uuid") or "") if isinstance(doc, dict) else "", "name": name,
            "size": len(data), "replaced": replaced}


def swap_owner(api, uuid: str, plan: dict) -> dict:
    """Write the owner change: the name and the person's own mailing address through the record's
    documented update, then one note. The record is read again afterwards, because whether that
    update renames the owner or attaches a new one is not ours to assume: if the owner now holding
    the new name still carries the deceased mark, the mark is cleared, since that person is alive."""
    api.call("/api/internal/property/%s/" % uuid, "PATCH", {"owner": plan["to"]})
    now = (api.call("/api/internal/property/%s/" % uuid) or {}).get("owner") or {}
    took = (str(now.get("first_name") or "").strip().lower() == plan["to"]["first_name"].lower()
            and str(now.get("last_name") or "").strip().lower() == plan["to"]["last_name"].lower())
    if took and now.get("deceased") and now.get("uuid"):
        api.call("/api/internal/owner/%s/" % now["uuid"], "PATCH", {"deceased": False})
    if took:
        api.call("/api/internal/property/%s/add-notes/" % uuid, "POST", {"notes": plan["note"]})
    return {"ok": took, "owner_uuid": now.get("uuid") or "", "same_owner": now.get("uuid") == plan["before"].get("uuid"),
            "skiptrace_attempts": int(now.get("skiptrace_attempts") or 0)}


# -------------------------------------------------------------- heir shares --
# Tennessee intestate succession, in plain words (T.C.A. 31-2-104 and 31-2-106):
#   a surviving spouse with no descendants takes everything; with descendants, the greater of one
#   third or a child's share; the rest goes to the children equally; a child who died first is
#   stood in for by that child's own children, who split that one share (by representation). If
#   no child outlived the owner, the grandchildren share equally. With no spouse and no
#   descendants it goes to the parents, then to brothers and sisters the same way.

_KIN = (("grandchild", r"\bgrand[\s-]?(son|daughter|child|children)\b"),
        ("grandparent", r"\bgrand[\s-]?(mother|father|parent)\b"),
        ("spouse", r"\b(wife|husband|spouse|widow|widower)\b"),
        ("child", r"\b(son|daughter|child)\b"),
        ("parent", r"\b(mother|father|parent)\b"),
        ("sibling", r"\b(brother|sister|sibling)\b"),
        ("niece_nephew", r"\b(niece|nephew)\b"))
_NOT_KIN = re.compile(r"in[- ]law|\bstep|\b(ex|former)[- ]?(wife|husband|spouse)\b|'s (wife|husband|spouse|partner)\b|"
                      r"\bgod(son|daughter|child)", re.I)
_GREAT = re.compile(r"\bgreat[\s-]?grand", re.I)
_FIDUCIARY = re.compile(r"\b(personal representative|executor|executrix|administrator|administratrix|trustee|"
                        r"attorney in fact|power of attorney|conservator|guardian)\b", re.I)
_KIN_AFTER_ROLE = re.compile(r"^\W*(?:the\s+)?(?:co-?)?(?:personal representative|executor|executrix|administrator|"
                             r"administratrix|trustee)\W+(?:and\s+)?(?:(?:the|her|his)\s+)?((?:[\w'-]+\W+)?[\w'-]+)", re.I)
_EXECUTOR = re.compile(r"\bexecut(?:or|rix)\b", re.I)
_WILL = re.compile(r"\b(last will|will and testament|probated will|unprobated will|under (?:his|her|the) will|testate|"
                   r"letters testamentary|devised|devisees?|left a will|named in (?:his|her|the) will)\b", re.I)
_NO_WILL = re.compile(r"\b(no will|without a will|intestate|no known will)\b", re.I)
# In Tennessee the mortgage itself is a "deed of trust" with a "trustee", and a foreclosure is a
# "substitute trustee's sale": 14 of the first 28 files on this machine said trust or trustee in the
# vesting line for that reason alone. Those words are set aside before a trust is looked for.
_MORTGAGE = re.compile(r"deeds? of trust|trust deeds?|(?:substitute|successor) trustee'?s?(?: sale| deed)?|"
                       r"trustee'?s? (?:sale|deed)", re.I)
_TRUST_TITLE = re.compile(r"\b(?:revocable|irrevocable|living|family|testamentary|land|inter vivos|special needs)\s+trust\b|"
                          r"\btrust\s+(?:dated|agreement)\b|\btrustees?\s+of\s+(?:the\s+)?[\w .'-]{0,60}?\btrust\b", re.I)
_TRUST_OWNER = re.compile(r"\b(trust|trustees?)\b", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}


def in_trust(vesting: str, owner_name: str = "") -> bool:
    """Is the house held in a trust? By how the title reads (a revocable, living or family trust, a
    trust dated, a trustee of a named trust) or by the owner's own name ending in Trust."""
    return bool(_TRUST_TITLE.search(_MORTGAGE.sub(" ", vesting or ""))
                or _TRUST_OWNER.search(_MORTGAGE.sub(" ", owner_name or "")))


def _kin(text: str) -> str:
    t = (text or "").lower()
    if not t.strip() or _NOT_KIN.search(t) or _GREAT.search(t):
        return ""
    return next((cls for cls, rx in _KIN if re.search(rx, t)), "")


def kin_class(head: str, full: str = "") -> str:
    """spouse, child, grandchild, parent, sibling, niece_nephew, fiduciary or other, from the short
    relationship. In-laws, step-relatives and former spouses are `other`: they do not inherit
    without a will. A representative is kin only where the tie is written right beside the role."""
    k = _kin(head)
    if k:
        return k
    if _FIDUCIARY.search(head or ""):
        m = _KIN_AFTER_ROLE.match(full or "")
        return (_kin(m.group(1)) if m else "") or "fiduciary"
    return "other"


def _date_key(s: str):
    s = (s or "").strip().lower()
    m = re.search(r"\b((?:19|20)\d\d)-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/((?:19|20)\d\d)\b", s)
    if m:
        return int(m.group(3)), int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+((?:19|20)\d\d)\b", s)
    if m and m.group(1) in _MONTHS:
        return int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))
    return None


def _frac(f: Fraction) -> str:
    return "%d/%d" % (f.numerator, f.denominator) if f.denominator != 1 else str(f.numerator)


def heir_shares(an: dict, research: dict | None = None, verified: dict | None = None) -> dict | None:
    """Each heir's share of a dead owner's house, for an offer sheet (plan 3.9).

    None when fewer than two people sign, and when the owner is alive: two living people on a
    title are two sellers, not heirs, and 19 of the first 28 files on this machine were exactly
    that. Otherwise a dict.

    The split is the Tennessee default with no will. It is computed only where the file leaves no
    room for a guess: the owner's death confirmed, no living co-owner on the title, no will or
    trust mentioned, every representative's family tie stated, and every grandchild's line known.
    Anything else returns computed False with the reason in words, because a wrong split printed
    on an offer is worse than a blank one. A title company confirms the split before closing."""
    an, research = an or {}, research or {}
    signers = signer_names(an)
    death = an.get("death") or {}
    says_dead = str(an.get("owner_status") or "").strip().lower() == "deceased"
    if len(signers) < 2 or not (death.get("owner_deceased") or says_dead):
        return None
    out = {"computed": False, "basis": "", "decedent": "", "of": "", "heirs": [], "notes": [], "reason": "",
           "signers": signers,
           "caution": "The default split when no will or trust says otherwise. A title company confirms it before closing."}

    def stop(reason: str) -> dict:
        out["reason"] = reason
        return out

    if not death.get("owner_deceased"):
        return stop("The research says the owner has died, but the death is not confirmed by an obituary opened in "
                    "the research, so no estate is divided yet.")
    decedent = plain(death.get("decedent")) or plain(research.get("owner_of_record"))
    out["decedent"] = decedent
    owner_dod = _date_key(death.get("dod") or "")
    dods = {plain(x.get("name")): x.get("dod") or "" for x in research.get("decedents") or []}

    fam = []
    for p in an.get("people") or []:
        name = plain(p.get("name"))
        if not name or (decedent and p.get("living") == "Deceased" and same_name(name, decedent, strict=True)):
            continue
        full = p.get("relationship") or ""
        head = (p.get("tag") or "").strip() or re.split(r"[(,.;]", full)[0].strip()
        fam.append({"name": name, "head": head.lower(), "full": full, "cls": kin_class(head, full),
                    "co_owner": p.get("role") == "owner", "living": p.get("living") or "Unknown",
                    "heir_of": plain(p.get("heir_of")),
                    "dod": next((v for k, v in dods.items() if same_name(k, name, strict=True)), "")})
    for x in research.get("decedents") or []:
        name = plain(x.get("name"))
        # strict: a dead son who carried his father's name is family, not the owner over again
        if (not name or RULED_OUT.search(x.get("name") or "") or (decedent and same_name(name, decedent, strict=True))
                or any(same_name(name, f["name"], strict=True) for f in fam)):
            continue
        full = x.get("relationship_to_owner") or ""
        head = re.split(r"[(,.;]", full)[0].strip()
        fam.append({"name": name, "head": head.lower(), "full": full, "cls": kin_class(head, full), "co_owner": False,
                    "living": "Deceased", "heir_of": "", "dod": x.get("dod") or ""})

    def alive(f):
        return f["living"] != "Deceased"

    def outlived(f):
        when = _date_key(f["dod"])
        return bool(owner_dod and when and when > owner_dod)

    co = [f["name"] for f in fam if f["co_owner"] and alive(f)]
    if co:
        return stop("%s is on the title and living. Whether the deceased owner's share passed to the co-owner at death "
                    "or to the heirs depends on the wording of the last deed, so the split is not computed."
                    % " and ".join(co))
    title = an.get("title") or {}
    if in_trust(str(title.get("vesting") or ""), str(research.get("owner_of_record") or "")):
        return stop("The title is in a trust: the trustee signs, and the trust document sets who receives what.")
    texts = [str(title.get("vesting") or ""), str(an.get("headline") or ""), str(an.get("owner_status_note") or "")]
    texts += [str(t) for t in (title.get("flags") or [])] + [str(t) for t in (an.get("signing_flags") or [])]
    texts += [" ".join(str(m.get(k) or "") for k in ("status", "why", "short")) for m in an.get("must_sign") or []
              if isinstance(m, dict)]
    texts += [f["full"] for f in fam] + [str(t) for t in (research.get("red_flags") or [])]
    blob = " ".join(texts)
    will, no_will = _WILL.search(blob), _NO_WILL.search(blob)
    named_executor = [f["name"] for f in fam if alive(f) and _EXECUTOR.search(f["head"] + " " + f["full"])]
    if will and no_will:
        return stop("The file reads both ways on a will (%s, and %s). A person settles it before a split is shown."
                    % (will.group(0).lower(), no_will.group(0).lower()))
    if will or (named_executor and not no_will):
        return stop("The file points to a will (%s). Shares follow the will, which was not read."
                    % (will.group(0).lower() if will else "%s is named as executor" % named_executor[0]))
    if any(_GREAT.search(f["head"] + " " + f["full"]) and alive(f) for f in fam):
        return stop("A great-grandchild is listed. A split across three generations needs an attorney's read.")
    loose = [f["name"] for f in fam if f["cls"] == "fiduciary" and alive(f)]
    if loose:
        return stop("%s is named as a representative and the file does not say how they are related. A representative "
                    "who is also an heir changes the split." % " and ".join(loose))

    notes = out["notes"]

    def by(cls):
        return [f for f in fam if f["cls"] == cls]

    def lines_of(heads: list, issue: list, word: str, next_why: str):
        """Equal shares at one generation. A member who died before the owner is stood in for by
        their own children; one who died after the owner left that share to their own estate."""
        before = [h for h in heads if not alive(h) and not outlived(h)]
        got = [h for h in heads if alive(h) or outlived(h)]
        kids = [g for g in issue if alive(g)]
        if not got:
            if not kids:
                return [], ""
            if not heads:
                return None, ("%s listed, but nobody they descend from. The file must say whether their parents are living."
                              % ("Grandchildren are" if word == "child" else "Nieces and nephews are"))
            return [[(g, Fraction(1), "", next_why)] for g in kids], ""
        lines = []
        for h in got:
            if alive(h):
                lines.append([(h, Fraction(1), "", "")])
            else:
                notes.append("%s died after the owner, so that share passed into %s's own estate. The heirs of that "
                             "estate sign for it." % (h["name"], h["name"]))
                lines.append([(dict(h, name="Estate of " + h["name"]), Fraction(1), "", "")])
        placed: dict = {}
        for g in kids:
            if g["heir_of"] and any(same_name(g["heir_of"], h["name"]) for h in got):
                notes.append("%s takes no share directly: %s outlived the owner." % (g["name"], g["heir_of"]))
                continue
            if g["heir_of"]:
                hit = [h for h in before if same_name(g["heir_of"], h["name"])]
            else:
                hit = [h for h in before if name_tokens(h["name"]) and name_tokens(h["name"])[0] in _words(g["full"])]
                if not hit and len(before) == 1:
                    hit = before
            if len(hit) != 1:
                if before:
                    return None, "The file does not say which deceased %s %s descends from." % (word, g["name"])
                notes.append("%s takes no share: no %s of the owner they descend from is shown as deceased."
                             % (g["name"], word))
                continue
            placed.setdefault(hit[0]["name"], []).append(g)
        for h in before:
            mine = placed.get(h["name"]) or []
            if mine:
                if not (owner_dod and _date_key(h["dod"])):
                    notes.append("The split takes %s to have died before the owner; the file gives no dates to check. "
                                 "Had %s outlived the owner, that share would sit in %s's own estate."
                                 % (h["name"], h["name"], h["name"]))
                lines.append([(g, Fraction(1, len(mine)), h["name"], "takes the share of %s, who died first" % h["name"])
                              for g in mine])
            else:
                notes.append("%s died before the owner and no child of theirs is named. If %s left children, they take "
                             "that share and the split changes." % (h["name"], h["name"]))
        return lines, ""

    spouses = [f for f in by("spouse") if alive(f) or outlived(f)]
    if len(spouses) > 1:
        return stop("More than one person is listed as the owner's spouse (%s)." % ", ".join(f["name"] for f in spouses))
    issue_lines, problem = lines_of(by("child"), by("grandchild"), "child",
                                    "grandchild: no child of the owner outlived the owner, so the grandchildren share equally")
    if problem:
        return stop(problem)
    rows, rest = [], Fraction(1)
    if spouses:
        sp, m = spouses[0], len(issue_lines)
        share = Fraction(1) if not m else max(Fraction(1, 3), Fraction(1, m + 1))
        if not alive(sp):
            notes.append("%s died after the owner, so the spouse's share passed into that estate." % sp["name"])
            sp = dict(sp, name="Estate of " + sp["name"])
        rows.append((sp, share, "", "surviving spouse: %s" % ("everything, there are no descendants" if not m
                                                              else "the greater of one third or a child's share")))
        rest -= share
    if issue_lines:
        for line in issue_lines:
            rows += [(f, rest / len(issue_lines) * part, through, why or "child of the owner") for f, part, through, why in line]
    elif not spouses:
        folks = [f for f in by("parent") if alive(f)]
        if folks:
            rows += [(f, Fraction(1, len(folks)), "", "parent: no spouse and no descendants") for f in folks]
        else:
            sib_lines, problem = lines_of(by("sibling"), by("niece_nephew"), "brother or sister",
                                          "niece or nephew: no brother or sister outlived the owner")
            if problem:
                return stop(problem)
            if not sib_lines:
                return stop("No spouse, descendant, parent, brother or sister of the owner is listed, so the heirs "
                            "are more distant and need an attorney's read.")
            for line in sib_lines:
                rows += [(f, Fraction(1, len(sib_lines)) * part, through,
                          why or "brother or sister: no spouse, descendants or parents") for f, part, through, why in line]
    if sum((r[1] for r in rows), Fraction(0)) != 1:
        return stop("The shares did not add up to the whole, so none are shown.")
    takers = [r[0]["name"] for r in rows]
    for f in fam:
        if alive(f) and f["name"] not in takers and f["cls"] == "other" and any(same_name(f["name"], s) for s in signers):
            notes.append("%s (%s) is listed to sign but takes no share by law without a will."
                         % (f["name"], f["head"] or "no tie stated"))
    for f, share, through, why in rows:
        if f["living"] == "Unknown":
            notes.append("%s is not verified as living. If %s died before the owner, the split changes."
                         % (f["name"], f["name"]))
        v = next((v for n, v in (verified or {}).items() if same_name(n, f["name"])), None)
        out["heirs"].append({"name": f["name"], "relationship": f["head"], "fraction": _frac(share),
                             "share": round(float(share), 6), "living": f["living"], "through": through,
                             "verified": bool(v and v.get("verified")), "why": why})
    out.update(computed=True, basis="Tennessee intestacy, no will found, by representation", of="the property")
    return out
