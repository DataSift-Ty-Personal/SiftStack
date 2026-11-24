"""The Curative Title board (Ty, 2026-11-23): a house whose owner died and whose title passes to the
heirs with no probate opened, worked on its own SiftLine board, the whole heir tree at once, five
business days, and never a text.

    Research      waits until the heir tree names at least one living, verified heir with a number a
                  caller may dial, AND the heir map (the PDF) is on the record
    Ready for     ready, not started. Every tagged house is on the board at once (Ty, 2026-11-23: "we
      Marketing   probably want all of them inside of the Siftline board"), and the morning pass starts at
                  most daily_starts of them a business day, the longest-waiting first, so the calls ramp
                  up instead of landing on the caller all at once. A card here gets no task and no letter.
    Day 1..5      one full round a business day: every living verified heir with a usable number has
                  their best number dialed that day. An heir with no usable number never holds a day.
                  A house enters Day 1 only from Ready for Marketing, by the morning start.
    Heir Engaged  an heir talked with us (an answered call of a minute or more on an heir's number), or
                  the record became a lead: the day tasks stop and the team is told once
    Exhausted     five rounds and nobody engaged: one call task thirty days out

WHY ITS OWN BOARD. With no probate nobody holds letters, so nobody can sell the house alone: every
heir with a share signs (a deed of their share and a release, drawn by an attorney). The work is the
whole family at once, not one decision maker. The county board waits on one decision maker and
texts; the heir lane works one heir at a time and texts. Neither fits, and both skip a record that
carries the Curative tag, so one family is never worked by two programs.

NEVER TEXTS. No Smarter Contact, no texting app, nothing staged for one. Calls are tasks for the
curative caller; letters are a file a person uploads to the mail house. A text to an heir about a
house still in the family reads as a pitch to people who may not know the rest of their family has
been contacted.

THE CALL ORDER, which every task carries: research every heir first; call 1 is discovery (no
price, say plainly that we are local buyers, ask each relative who is who and who wants out); the
offer comes on call 2; walk away when someone who will not sell lives in the house; mail is last.

WHO THE HEIRS ARE comes from one of two places, and nothing here guesses: (a) the record's saved deep
prospect research on this box (its verified signers, heirlane.heirs_of), else (b) the heir file the
loader wrote (curative_setup.py load: a fact, and a file the box reads from <data>/curative/heirs).
A living heir is VERIFIED when the research says so; an heir whose living status is unknown is
counted and named in the note, never called as a signer.

ONE OWNER FOR EACH DECISION.
    the column a card is in        this stage; nothing else moves a card it placed
    whether a heir was called      the shared call log (an outbound dial inside calling hours)
    whether a heir engaged         the shared call log (an answered call of conversation_seconds)
    who may be touched             the gate, with this stage's allow words (gate_allow)
    the letters                    staged here as a file batch; released only by a person, twice
                                   checked (heirlane.release_roster)

A DAY WITHOUT ITS ROUND HOLDS THE CARD. A card moves only in a pass that runs before calls start,
judged on the business days that are over, so it never gains a column for a day still in progress,
never advances twice in one business day, and each business day earns one column at most. A move that
is not the round (to Heir Engaged, back to Research, to Ready for Marketing, the start into Day 1) is
never held back by that rule; the starts have their own daily cap.

PLAN MODE simulates the board in the autopilot's own memory (PLAN_FACT), so a plan run on the second
morning shows the moves the live board would make. Nothing reaches DataSift from a plan. A live card
create, move and removal, and every task, goes through ctx.write with the value it replaced recorded
first, so `autopilot undo <run>` puts a run back (leadroutes.undo_step; a card this stage created is
removed by undo_create below).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
from . import activity, gate, heirlane, lanes, ledger, leadroutes, limits, mail, researchroutes
from . import ftmpipeline as fp
from .stages import Ctx, addr, dial_order, marks_of, resolve_callers, status_of

log = logging.getLogger(__name__)

KIND = "curative"
KIND_MAIL = "curative_mail"
SERIES = "curative"                 # the series a curative letter is booked under in the ledger
TAG = "Curative"
BOARD = "Curative Title"
RESEARCH, READY, ENGAGED, EXHAUSTED = "Research", "Ready for Marketing", "Heir Engaged", "Exhausted"
DAYS = 5
DAY_COLUMNS = tuple("Day %d" % n for n in range(1, DAYS + 1))
COLUMNS = (RESEARCH, READY) + DAY_COLUMNS + (ENGAGED, EXHAUSTED)
ENGAGED_NO, EXHAUSTED_NO, READY_NO = DAYS + 1, DAYS + 2, DAYS + 3
STARTS_KV = "autopilot:curative:starts:%s:%s:%s"   # (live or plan, stage, business day) -> the houses started that day
FACT = "curative_card"              # the record's card as the live board holds it
PLAN_FACT = "curative_card_plan"    # where a plan run would have put it; read by this stage's plan runs only
OFF_FACT = "curative_card_off"      # a card a person took off the board: not put back on its own
TREE_FACT = "curative_heirs"        # the heir file the loader wrote (curative_setup.py load)
DOC_PREFIX = "curative-title-"      # the PDF's name on the record: curative-title-<slug>.pdf
ANSWERED = ("owner.call.answered", "owner.call.received")
# Gate findings that take a card off the board for good: nothing on the board may touch the record.
GONE_CODES = fp.GONE_CODES
ALERT = "An heir picked up on %s: work the one-heir call today."
METHOD = ("Curative title: every heir with a share signs. Call 1 is discovery, with no price: say plainly that you are "
          "a local buyer, and ask who is who in the family and who wants out. The offer comes on call 2. Walk away if "
          "someone who will not sell lives in the house.")

DEFAULTS = {
    "tag": TAG,                       # the door: records carrying this tag are on the board
    "board": BOARD,                   # the SiftLine board's title
    "column_ids": {},                 # column title -> uuid, printed by curative_setup.py board
    "caller": None,                   # the curative caller: a first name or {name, uuid}; else the playbook's callers
    "research_workflow": "deep_prospect",
    "heir_files": "",                 # where the loader's heir files sit on this box ("" is <data>/curative/heirs)
    "conversation_seconds": 60,       # an answered call this long on an heir's number is an heir engaged
    "daily_starts": 10,               # houses moved from Ready for Marketing into Day 1 a business day, longest-waiting first
    "exhausted_callback_days": 30,    # the one call task written when the card reaches Exhausted
    "daily_pieces": 0,                # letters staged a run; 0 while mail is held
    "letter_piece": "handwritten_letter",
    "letter_copy": None,              # one letter body (or a list, the first is used); LETTER otherwise
    "mailed_tag": "Mailed {yyyy}-{mm}",
    "last_mailed_field": "Last Mailed",
    "floor_days": 45,
    "max_log_calls": 400,
    "max_records": 100,               # full records opened a run
    "max_scan": 2000,                 # rows read through the tag
    "max_creates": 100,
    "max_moves": 200,
    "max_tasks": 200,
    "max_cards": 1000,                # cards listed per column
    "off_days": 30,                   # a card a person took off stays off this long
}
_COUNTS = ("conversation_seconds", "daily_starts", "exhausted_callback_days", "daily_pieces", "floor_days",
           "max_log_calls", "max_records", "max_scan", "max_creates", "max_moves", "max_tasks", "max_cards", "off_days")


def options(st) -> dict:
    """The stage block over the defaults above."""
    o = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k)
        if v is not None:
            o[k] = v
    o["column_ids"] = {str(k): str(v) for k, v in dict(o.get("column_ids") or {}).items() if v}
    return o


def block_problems(st) -> list[str]:
    """What is wrong with a curative stage's playbook block, in the playbook checker's words, so a typo
    fails `cli.py autopilot check` instead of reading as a default on the first live morning."""
    out = []
    for key in ("board", "tag"):
        v = st.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            out.append("stage %s: %s must name the %s" % (st.id, key, "SiftLine board" if key == "board" else "tag"))
    ids = st.get("column_ids")
    if ids is not None:
        if not isinstance(ids, dict):
            out.append("stage %s: column_ids must map a column title to its uuid" % st.id)
        else:
            bad = sorted(str(k) for k in ids if str(k) not in COLUMNS)
            if bad:
                out.append("stage %s: column_ids names %s; the board's columns are %s" % (st.id, bad, ", ".join(COLUMNS)))
    lane = st.get("lane")
    if lane is not None and str(lane) != "heirs":
        out.append("stage %s: the curative board works the heirs lane, not %s" % (st.id, lane))
    caller = st.get("caller")
    if caller is not None and not (isinstance(caller, str) and caller.strip()) \
            and not (isinstance(caller, dict) and caller.get("uuid")):
        out.append("stage %s: caller is a DataSift first name or {name, uuid}" % st.id)
    copy = st.get("letter_copy")
    if copy is not None and not (isinstance(copy, str) and copy.strip()) \
            and not (isinstance(copy, list) and copy and all(isinstance(x, str) and x.strip() for x in copy)):
        out.append("stage %s: letter_copy is one letter body (or a list of them)" % st.id)
    for key in _COUNTS:
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a whole number, 0 or more" % (st.id, key))
    if st.get("pipeline") is not None:
        out.append("stage %s: the curative board follows no other board (`pipeline` is for the county stages)" % st.id)
    return out


# ---------------------------------------------------------------------- columns
def column_no(title: str) -> int:
    """Research 0, Day 1 to Day 5 as 1 to 5, Heir Engaged 6, Exhausted 7, Ready for Marketing 8, anything
    else -1. The numbers name a column for the code; the board's own order is set when it is made."""
    t = str(title or "").strip()
    if t == RESEARCH:
        return 0
    if t in DAY_COLUMNS:
        return int(t.split()[1])
    if t == ENGAGED:
        return ENGAGED_NO
    if t == EXHAUSTED:
        return EXHAUSTED_NO
    if t == READY:
        return READY_NO
    return -1


def column_title(n: int) -> str:
    """The column a round count lands in: 0 Research, 1 to 5 a day, past the fifth day Exhausted."""
    if n <= 0:
        return RESEARCH
    return "Day %d" % n if n <= DAYS else EXHAUSTED


# ---------------------------------------------------------------------- words about an heir
_KIN = (
    (r"\bstep[- ]?(?:son|daughter|child)", "Stepchild"),
    (r"\b(?:wife|husband|spouse|widow|widower)\b", "Spouse"),
    (r"\bgreat[- ]?grand[- ]?(?:son|daughter|child)", "Great Grandchild"),
    (r"\b(?:grand|great)[- ]?nephew", "Grandnephew"),
    (r"\b(?:grand|great)[- ]?niece", "Grandniece"),
    (r"\bgrand[- ]?son\b", "Grandson"),
    (r"\bgrand[- ]?daughter\b", "Granddaughter"),
    (r"\bgrand[- ]?(?:child|children)\b", "Grandchild"),
    (r"\bson\b", "Son"),
    (r"\bdaughter\b", "Daughter"),
    (r"\b(?:child|children)\b", "Child"),
    (r"\bnephew\b", "Nephew"),
    (r"\bniece\b", "Niece"),
    (r"\bbrother\b", "Brother"),
    (r"\bsister\b", "Sister"),
    (r"\bsibling\b", "Sibling"),
    (r"\bcousin\b", "Cousin"),
    (r"\bmother\b", "Mother"),
    (r"\bfather\b", "Father"),
    (r"\bparent\b", "Parent"),
    (r"\baunt\b", "Aunt"),
    (r"\buncle\b", "Uncle"),
)


def relationship_head(text: str) -> str:
    """The heir's tie in a few words, lower case, with the name it is a tie to cut off: research writes
    "son of <the owner's full name>", and a task title, a note or a phone tag must never carry a name
    it does not need."""
    head = re.split(r"\s+of\s+|[(,;:]", str(text or ""), maxsplit=1)[0]
    return " ".join(head.split()).lower()


def relationship_tag(text: str) -> str:
    """The phone tag for an heir's tie (Daughter, Grandson, Spouse ...), Relative when it says none.
    A tag carries the tie only: never a name."""
    head = relationship_head(text)
    for rx, tag in _KIN:
        if re.search(rx, head):
            return tag
    return "Relative"


def share_text(raw) -> str:
    """'1/3' from '1/3', '1/17 (working)' or 0.3333; '' when there is no share to show."""
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(raw or ""))
    if m:
        return "%s/%s" % (m.group(1), m.group(2))
    return ""


def legal_name(name: str) -> str:
    """The name as a person would sign it: research's asides in brackets or quotes taken out."""
    return " ".join(re.sub(r"\(.*?\)|\".*?\"", " ", str(name or "")).split())


def tier_from_activity(score) -> str:
    """The house tier for a Trestle activity score (phone_validator.DEFAULT_TIERS): 81 to 100 Dial First,
    61 Dial Second, 41 Dial Third, 21 Dial Fourth, below that Drop. '' for no score."""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return ""
    for floor, tier in ((81, "Dial First"), (61, "Dial Second"), (41, "Dial Third"), (21, "Dial Fourth")):
        if score >= floor:
            return tier
    return "Drop"


# ---------------------------------------------------------------------- the heir tree
@dataclass
class Heir:
    key: str
    name: str
    first: str = ""
    relationship: str = ""          # in a few words, lower case ("daughter")
    share: str = ""                 # "1/3", or ""
    living: bool | None = None
    verified: bool = False
    numbers: list = field(default_factory=list)      # [{"number", "tier", "type"}], best first
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""

    def mailable(self) -> bool:
        return bool(self.street and self.city and len(self.state) == 2 and re.fullmatch(r"\d{5}", self.zip or ""))

    def digits(self) -> list[str]:
        return [n for n in (gate.digits((x or {}).get("number")) for x in self.numbers) if n]

    def label(self) -> str:
        """'Kim (daughter, 1/3 share)': how a task or a line names an heir."""
        bits = [b for b in (self.relationship, ("%s share" % self.share) if self.share else "") if b]
        return "%s (%s)" % (self.first or self.name, ", ".join(bits)) if bits else (self.first or self.name)


def heirs_of_tree(tree: dict | None) -> list[Heir]:
    out = []
    for h in (tree or {}).get("heirs") or []:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        name = str(h["name"])
        living = h.get("living")
        out.append(Heir(key=str(h.get("key") or heirlane.heir_key(name)), name=name,
                        first=str(h.get("first") or mail._clean_first(name) or ""),
                        relationship=str(h.get("relationship") or ""), share=share_text(h.get("share")),
                        living=living if isinstance(living, bool) else None, verified=bool(h.get("verified")),
                        numbers=[dict(n) for n in h.get("numbers") or [] if isinstance(n, dict) and gate.digits(n.get("number"))],
                        street=str(h.get("street") or ""), city=str(h.get("city") or ""),
                        state=str(h.get("state") or "").strip().upper(), zip=str(h.get("zip") or "").strip()[:5]))
    return out


def pdf_on(tree: dict | None) -> bool:
    return bool(((tree or {}).get("pdf") or {}).get("filed"))


def heir_dir(o: dict) -> Path:
    return Path(str(o.get("heir_files") or "")) if o.get("heir_files") else Path(config.DATA_DIR) / "curative" / "heirs"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def loaded_tree(u: str, folder: Path) -> dict | None:
    """The loader's heir file for this record: the fact, or the file the loader wrote for this box (it
    runs on the workstation, which keeps its own memory). A newer file replaces the fact; the file is
    the autopilot's own memory, so a plan run keeps it too."""
    f = ledger.fact(u, TREE_FACT)
    disk = _read_json(Path(folder) / ("%s.json" % u)) if u else None
    if isinstance(disk, dict) and isinstance(disk.get("heirs"), list) and str(disk.get("record_uuid") or "") == u:
        if not isinstance(f, dict) or str(disk.get("loaded") or "") > str(f.get("loaded") or ""):
            ledger.set_fact(u, TREE_FACT, disk)
            f = disk
    return f if isinstance(f, dict) and isinstance(f.get("heirs"), list) else None


def _run_dir(u: str, workflow: str) -> Path | None:
    from . import researchqueue
    res = researchqueue.last_result(u, workflow) or {}
    p = Path(str(res.get("run_dir") or "")) if res.get("run_dir") else None
    return p if p is not None and p.is_dir() else None


def _research_pdf(run_dir: Path | None, slug: str) -> dict | None:
    """Was the deep prospect's PDF filed on the record? The write-back keeps what it filed in the run
    folder (writeback_audit.json): filed now, or already there unchanged."""
    if run_dir is None or not slug:
        return None
    entry = (_read_json(run_dir / "writeback_audit.json") or {}).get(slug) or {}
    doc = entry.get("document") if isinstance(entry, dict) else None
    if isinstance(doc, dict) and (doc.get("filed") or "already filed" in str(doc.get("why") or "")):
        return {"name": str(doc.get("name") or ""), "filed": True, "at": ""}
    return None


def _research_shares(run_dir: Path | None, slug: str, heirs: list) -> dict:
    """heir key -> share, when the research's own split was computed (writeback.heir_shares: the
    Tennessee default with no will, only where the file leaves no room for a guess)."""
    if run_dir is None or not slug:
        return {}
    an = _read_json(run_dir / "analysis" / ("%s.json" % slug))
    if not isinstance(an, dict):
        return {}
    rs = _read_json(run_dir / "research" / ("%s.json" % slug))
    try:
        from ..research.writeback import heir_shares
        split = heir_shares(an, rs if isinstance(rs, dict) else {}, None) or {}
    except Exception:  # noqa: BLE001  (a split that cannot be worked out is shown as no share, never guessed)
        return {}
    if not split.get("computed"):
        return {}
    out = {}
    for row in split.get("heirs") or []:
        for h in heirs:
            if heirlane.same_person(h.name, str(row.get("name") or "")):
                out[h.key] = str(row.get("fraction") or "")
    return out


def research_tree(u: str, rec: dict, workflow: str) -> dict | None:
    """The tree from the record's own deep prospect on this box: its verified signers (heirlane.heirs_of),
    each number with the tier the record's phone carries, the research's own share split when it was
    computed, and whether its PDF is on the record. None when this box holds no research naming a
    verified signer."""
    research = heirlane.load_research(u, workflow)
    if not research:
        return None
    signers, _ = heirlane.heirs_of(research)
    if not signers:
        return None
    run_dir = _run_dir(u, workflow)
    slug = str(research.get("slug") or "")
    shares = _research_shares(run_dir, slug, signers)
    phones = {gate.digits(p.get("number")): p for p in ((rec.get("owner") or {}).get("phones") or [])}
    heirs = []
    for h in signers:
        nums = [{"number": n, "tier": gate.phone_tier(phones.get(n) or {}),
                 "type": str((phones.get(n) or {}).get("type") or "").upper()} for n in h.numbers]
        heirs.append({"key": h.key, "name": h.name, "first": h.first, "relationship": relationship_head(h.relationship),
                      "share": shares.get(h.key, ""), "living": True, "verified": True, "numbers": nums,
                      "street": h.street, "city": h.city, "state": h.state, "zip": h.zip})
    return {"source": "research", "record_uuid": u, "slug": slug, "heirs": heirs,
            "pdf": _research_pdf(run_dir, slug), "call_first": heirs[0]["key"]}


# ---------------------------------------------------------------------- the letter, and its audit
LETTER = ("Hi {first}, my name is {sender}, and I am a local buyer. I am writing about the house at {street}. "
          "If you or your family would ever consider selling it, I would be glad to talk, with no cost and no pressure. "
          "I can buy it as it sits and work around everyone's schedule. You can call or text me at {phone}.")
# Words a letter to an heir never carries: legal matters (the heirs get their own attorney's word, never
# ours), a claim to act for anyone, a price.
CLAIM_WORDS = re.compile(r"\b(attorney|lawyer|legal|lawsuit|court|title|deed|represent\w*|on behalf|behalf of|"
                         r"guarantee\w*|offer of|cash offer|dollars?|thousand)\b", re.I)
PRICE = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\s?[kK]\b")


def letter_body(o: dict) -> str:
    copy = o.get("letter_copy")
    if isinstance(copy, list):
        copy = next((str(x) for x in copy if str(x).strip()), "")
    return str(copy).strip() if isinstance(copy, str) and copy.strip() else LETTER


def render_letter(body: str, first: str, street: str, sender: str, phone: str) -> str:
    return " ".join(body.format(first=first or "there", street=street, sender=sender, phone=phone).split())


SAMPLE_STREET = "12 Sample St"


def audit_letter(text: str, street: str = "") -> list[str]:
    """Everything wrong with one curative letter: the heir lane's audit (no death word, never the list,
    no dash, no link, no dollar sign, no zip, no capitals, nothing left blank) plus no legal matter,
    no claim to represent anyone and no price. The house's own street is not copy: it is read as a
    sample street, so a house on Country Estates Dr can still get a letter."""
    t = str(text or "")
    if street:
        t = t.replace(street, SAMPLE_STREET)
    out = heirlane.audit_copy(t, street=SAMPLE_STREET if street else "", sms=False)
    m = CLAIM_WORDS.search(t)
    if m:
        out.append("names a legal matter, a claim or a price (%r)" % m.group(0).lower())
    m = PRICE.search(t.replace(SAMPLE_STREET, " "))
    if m:
        out.append("names a figure that reads as a price (%r)" % m.group(0))
    return out


def audit_setting(o: dict) -> list[str]:
    """The letter the stage would send, filled with sample values and audited. Nothing is staged while
    this is not empty."""
    try:
        text = render_letter(letter_body(o), "Kim", "12 Oak St", "Dana", "865-555-0100")
    except (KeyError, IndexError, ValueError) as exc:
        return ["the letter has a blank the board cannot fill (%s)" % exc]
    return audit_letter(text, street="12 Oak St")


# ---------------------------------------------------------------------- the round and its evidence
def dialed_by_day(owner_uuid: str, since: datetime, zone, hours) -> dict:
    """business-local date -> the numbers dialed out to this owner that day inside calling hours. The
    call log files every call under the OWNER, and the heirs' numbers sit on the owner of the record."""
    out: dict = {}
    if not owner_uuid:
        return out
    for e in activity.events(activity.DIAL, resource=owner_uuid, since=since):
        if e["data"].get("direction") == "inbound":
            continue
        local = e["when"].astimezone(zone)
        m = local.hour * 60 + local.minute
        if hours[0] <= m < hours[1]:
            out.setdefault(local.date(), set()).add(e["data"].get("number") or "")
    return out


def engaging_call(owner_uuid: str, numbers: set, since: datetime, seconds: int) -> dict | None:
    """The first answered call, either way, of `seconds` or more on one of these numbers since `since`."""
    if not owner_uuid or not numbers:
        return None
    for e in activity.events(ANSWERED, resource=owner_uuid, since=since):
        d = e["data"]
        if d.get("number") in numbers and int(d.get("duration") or 0) >= int(seconds):
            return e
    return None


def need_of(heirs: list, usable: list) -> list:
    """(heir, best number) for every living verified heir with a number a caller may dial now. `usable`
    is the gate's list for the record ({number, tier, status, type}); best is the house dial order."""
    by = {gate.digits(x.get("number")): x for x in usable or []}
    out = []
    for h in heirs:
        if h.living is not True or not h.verified:
            continue
        mine = [by[n] for n in h.digits() if n in by]
        if mine:
            out.append((h, dial_order(mine)[0]["number"]))
    return out


def round_done(day: date, need: list, dialed: dict, bad: set) -> bool:
    """Was this business day's round in? At least one heir has a usable number, and every such heir had
    their best number dialed that day. A number of theirs that is marked wrong or dead since, dialed
    that day, counts: it was the best number when the caller dialed it."""
    if not need:
        return False
    got = dialed.get(day) or set()
    for h, best in need:
        if best in got or any(n in got and n in bad for n in h.digits()):
            continue
        return False
    return True


def judge(n: int, due: date, until: date, done, days) -> tuple[int, list]:
    """(the round count the evidence earns, the business days missed). Walks the business days from the
    column's due day through `until`; each day whose round is in (`done(day)`) earns one column. A
    sixth column is Exhausted."""
    misses: list = []
    d = due
    while d <= until and 1 <= n <= DAYS:
        if d.weekday() in days:
            if done(d):
                n += 1
            else:
                misses.append(d)
        d += timedelta(days=1)
    return n, misses


# ---------------------------------------------------------------------- the gate, as this board reads it
def _tag_of(o: dict) -> str:
    return str(o.get("tag") or TAG).strip().lower()


def other_research_tags(ctx: Ctx, rec: dict, o: dict) -> list[str]:
    """Research tags on the record other than this board's own (gate.research_tags; Tax Sale Redemption
    in the live playbook). The board never works a record one of those holds."""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    marks, mine = marks_of(rec), _tag_of(o)
    return [str(t).strip() for t in cfg.research_tags or [] if str(t).strip().lower() in marks
            and str(t).strip().lower() != mine]


def extra_allow(ctx: Ctx, rec: dict, o: dict) -> tuple:
    """The allow word this record needs beyond the stage's own gate_allow: research_tag, only when the
    one research tag on it is this board's (the playbook may list Curative among the research tags, so
    every other stage keeps off these records). Another research tag is never lifted from here."""
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    mine = _tag_of(o)
    listed = {str(t).strip().lower() for t in cfg.research_tags or []}
    if mine in listed and mine in marks_of(rec) and not other_research_tags(ctx, rec, o):
        return ("research_tag",)
    return ()


def assess(ctx: Ctx, rec: dict, row: dict | None, o: dict) -> tuple[str, str]:
    """(gone | lead | hold | exhausted | research | work, why) for one full record.

    gone       nothing on the board may touch it again: no longer tagged, a company or a trust owns it,
               or the gate's hard holds (Do Not Market, a litigator, an opt-out, sold, a finished status)
    lead       a lead status: the card goes to Heir Engaged and nothing else is done
    hold       the card stays where it is and nothing is written today (the owner is a lead on another
               record, a status the gate does not know, another program's research tag)
    exhausted  the record's own status says the round is over
    research   the round cannot run yet (held for research by status, or Mail Only)"""
    if _tag_of(o) not in marks_of(rec):
        return "gone", "no longer tagged %s" % (o.get("tag") or TAG)
    s = status_of(rec)
    if s in lanes.LEAD_STATUSES:
        return "lead", "status %s" % s
    from deep_prospect_batch import is_entity       # late: the board only needs it for a record it reads
    if is_entity(rec.get("owner") or {}):
        return "gone", "a company or a trust owns it (the board works people's families only)"
    v = ctx.verdict(rec, "call", row=row, allow=extra_allow(ctx, rec, o))
    codes = {c: d for c, d in v.reasons}
    for c in GONE_CODES:
        if c in codes:
            return "gone", gate.words(c, codes[c])
    others = other_research_tags(ctx, rec, o)
    if others:
        return "hold", "held for research (tag %s)" % others[0]
    if "lead" in codes:
        return "lead", gate.words("lead", codes["lead"])
    if "owner_lane" in codes:
        return "hold", "the owner is in the %s lane on another record" % codes["owner_lane"]
    if "unknown_status" in codes:
        return "hold", gate.words("unknown_status", codes["unknown_status"])
    if "deceased" in codes:
        return "hold", "the gate holds a dead owner's record for this stage: gate_allow on the stage needs deceased"
    if s == "exhausted":
        return "exhausted", "status Exhausted"
    if "in_research" in codes:
        return "research", "held for research (%s)" % (codes["in_research"] or "status Deep Prospecting")
    if "mail_only" in codes:
        # The tag was set for the owner of record, who had no number. The heirs have their own: a person
        # takes the tag off, or the stage's gate_allow names mail_only, once the heirs' numbers are on it.
        return "research", "tagged Mail Only, so no heir is called (take the tag off, or add mail_only to gate_allow)"
    return "work", ""


# ---------------------------------------------------------------------- what other code asks
def position(record_uuid: str) -> dict | None:
    """The record's card as the live board holds it."""
    f = ledger.fact(record_uuid, FACT) if record_uuid else None
    return f if isinstance(f, dict) and f.get("column") else None


def undo_create(crm, change: dict) -> str:
    """Put back a card this stage created: the card goes, from wherever it sits now. leadroutes.undo_step
    sends a curative card here (the change's `after` carries kind "curative"); the card's id is in the
    run's ledger event."""
    u = change.get("record_uuid") or ""
    ids = [(e["detail"].get("card"), e["detail"].get("column")) for e in ledger.events(u, action="card_create")
           if e["run_id"] == change.get("run_id") and e["detail"].get("card")]
    if not ids:
        raise RuntimeError("the card's id was not recorded, so it cannot be removed from here")
    now = position(u) or {}
    for card, col in ids:
        places = [now.get("column_uuid") if now.get("card") == card else "", col, (change.get("after") or {}).get("column")]
        for where in dict.fromkeys(p for p in places if p):
            if leadroutes.card_in_column(crm, where, card):
                leadroutes.remove_card(crm, where, card)
                break
        else:
            raise RuntimeError("the card is not in the column it was placed in or last moved to; take it off by hand")
        if now.get("card") == card:
            ledger.set_fact(u, FACT, None)
    return "%d card(s) removed" % len(ids)


def _facts(key: str) -> dict:
    """record uuid -> the card fact, for every record that has one, in one read."""
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (key,)).fetchall():
            try:
                v = json.loads(r["value"])
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("column"):
                out[r["record_uuid"]] = v
    return out


def _nice(d: date) -> str:
    return d.strftime("%a %b %d").replace(" 0", " ")


def street_of(rec: dict) -> str:
    return str(((rec or {}).get("address") or {}).get("street") or "") or addr(rec)


# ---------------------------------------------------------------------- one pass over the board
class Run:
    """One pass over the board. Holds what the run read so the decisions below share it."""

    def __init__(self, ctx: Ctx, o: dict):
        self.ctx, self.o, self.st = ctx, o, ctx.stage
        self.zone = ZoneInfo(ctx.policy.tz)
        local = ctx.now.astimezone(self.zone)
        self.today = local.date()
        self.today_s = self.today.isoformat()
        self.days = ctx.policy.days
        self.lim = limits.settings(ctx.policy)
        self.hours = self.lim.call_hours
        self.first_call = int(self.hours[0])
        self.morning = local.hour * 60 + local.minute < self.first_call
        self.key = FACT if ctx.live else PLAN_FACT
        self.heir_dir = heir_dir(o)
        self.cols: dict = {}
        self.board_uuid = ""
        self.pos: dict = {}           # record -> card fact
        self.rows: dict = {}          # record -> search row (the tag door)
        self.reads = 0
        self.creates = self.moves = self.tasks = self.cleared = 0
        self.broken = False
        self.verified = False
        self.tally = ctx.out.tally
        self.placed: Counter = Counter()
        self.moved: Counter = Counter()
        self.held: Counter = Counter()
        self.off: Counter = Counter()
        self.short: Counter = Counter()
        self.gone: dict = {}          # record -> why its staged letters come back out
        self.owed: list = []          # (record uuid, record, row, heirs) whose letters are owed
        self.sources: Counter = Counter()
        self.caller = None
        self.engaged: list = []
        # (owner, number) given a call task this run: one family can own two curative houses, and the
        # call log files both under the one owner, so one dial covers both cards and one task is enough.
        self.tasked: set = set()
        # Ready for Marketing cards that may start this run: (record, card fact, record, row, heirs, need).
        self.ready: list = []
        self.started: list = []       # streets started into Day 1 this run

    # ---- reading
    def sync(self) -> None:
        for group in ("calls", "state", "board"):
            s = activity.sync(self.ctx.crm, group, now=self.ctx.now, floor_days=int(self.o["floor_days"]),
                              max_calls=int(self.o["max_log_calls"]))
            if not s["complete"]:
                self.ctx.out.problem("the %s log is still catching up (read through %s); a day with no call shown "
                                     "holds, and the next run reads further" % (group, s["through"] or "nothing yet"))

    def load_board(self) -> bool:
        """The board's columns, by title: from the playbook when it names all nine, else learned. A live
        run with any column unknown does nothing (so this code before the Ready for Marketing id is in the
        playbook leaves the board exactly as it is); a plan run goes on and simulates the board."""
        o, ctx = self.o, self.ctx
        given = dict(o["column_ids"])
        problems: list = []
        b = leadroutes.load_board(ctx.crm, str(o["board"]), column_ids=given, expect=COLUMNS, now=ctx.now,
                                  problems=problems)
        self.board_uuid = b.uuid
        for title in COLUMNS:
            uid = given.get(title) or b.find(title)
            if uid:
                self.cols[title] = uid
        missing = [t for t in COLUMNS if t not in self.cols]
        if not missing:
            return True
        say = ("the %s board's columns %s are not known: run python src/agents/autopilot/curative_setup.py board, then "
               "put each column's id under column_ids in this stage's block" % (o["board"], ", ".join(missing)))
        if ctx.live:
            ctx.out.problem(say)
            for p in problems:
                ctx.out.problem(p)
            ctx.out.head = "The curative board's columns are not known yet, so no card was placed or moved."
            return False
        self.tally["plan only: the board is simulated (%s)" % ("its columns are not set yet" if self.cols else
                                                                "it does not exist yet")] += 1
        return True

    def read_cards(self) -> None:
        """Where every card is now. Live: the board's own listing is the truth, merged with what this stage
        remembers. A card a person moved is taken where they put it; a card a person removed is not put
        back. Plan: the simulation, with any real card on the board taking its place."""
        ctx, o = self.ctx, self.o
        mine = _facts(self.key)
        found: dict = {}
        unread: set = set()
        if len(self.cols) == len(COLUMNS):
            by_card = {v.get("card"): u for u, v in _facts(FACT).items() if v.get("card")}
            index = leadroutes.card_index()
            for title, col in self.cols.items():
                try:
                    listed = leadroutes.cards(ctx.crm, col, cap=int(o["max_cards"]))
                except Exception as exc:  # noqa: BLE001
                    ctx.out.problem("could not list the %s column of the curative board (%s)" % (title, str(exc)[:100]))
                    unread.add(title)
                    continue
                if len(listed) >= int(o["max_cards"]):
                    unread.add(title)
                    ctx.out.problem("the %s column of the curative board holds %d or more cards, the most one run lists; "
                                    "raise max_cards" % (title, int(o["max_cards"])))
                for c in listed:
                    cu = str(c.get("uuid") or "")
                    u = leadroutes.card_prop(c) or by_card.get(cu) or index.get(cu, "")
                    if not cu:
                        continue
                    if not u:
                        self.tally["cards whose record the stage cannot name (left alone)"] += 1
                        continue
                    found[u] = {"card": cu, "column": title, "column_uuid": col}
        if ctx.live:
            for u, f in mine.items():
                if u not in found and f.get("column") in unread:
                    self.pos[u] = f
                elif u not in found:
                    ledger.set_fact(u, OFF_FACT, {"day": self.today_s, "column": f.get("column")})
                    ledger.set_fact(u, FACT, None)
                    self.tally["cards a person took off the board (not put back for %d days)" % int(o["off_days"])] += 1
            for u, c in found.items():
                f = dict(mine.get(u) or {})
                if f.get("card") == c["card"] and f.get("column") == c["column"]:
                    f["column_uuid"] = c["column_uuid"]
                    self.pos[u] = f
                    continue
                if f:
                    self.tally["cards a person moved (taken where they put them)"] += 1
                entered = self._entered(u, c)
                f.update(card=c["card"], column=c["column"], column_uuid=c["column_uuid"],
                         entered=entered.isoformat(timespec="seconds"),
                         due=fp.due_day(entered, self.zone, self.days, self.first_call).isoformat(),
                         placed=f.get("placed") or entered.isoformat(timespec="seconds"),
                         source="moved by a person" if f else "found on the board")
                ledger.set_fact(u, FACT, f)
                self.pos[u] = f
            return
        self.pos = dict(mine)
        for u, c in found.items():
            f = dict(self.pos.get(u) or {})
            if f.get("column") != c["column"]:
                entered = self._entered(u, c)
                f.update(card=c["card"], column=c["column"], column_uuid=c["column_uuid"],
                         entered=entered.isoformat(timespec="seconds"),
                         due=fp.due_day(entered, self.zone, self.days, self.first_call).isoformat(),
                         placed=f.get("placed") or entered.isoformat(timespec="seconds"), source="on the board")
            self.pos[u] = f

    def _entered(self, u: str, c: dict) -> datetime:
        e = leadroutes.last_card_event(u)
        if e and str(e["data"].get("column") or "").strip().lower() in (c["column"].lower(), c["column_uuid"].lower()):
            return e["when"]
        if self.reads < int(self.o["max_records"]):
            try:
                tl = leadroutes.timeline(self.ctx.crm, c["column_uuid"], c["card"])
                self.reads += 1
                cur = tl.get(c["column_uuid"]) or {}
                if cur.get("seconds") is not None:
                    return self.ctx.now - timedelta(seconds=float(cur["seconds"]))
            except Exception:  # noqa: BLE001  (an unread timeline means "now", which only delays the card a day)
                pass
        return self.ctx.now

    def read_rows(self) -> None:
        """The door: every record carrying the board's tag."""
        tag = str(self.o["tag"])
        tid = self.ctx.crm.tag_uuid(tag)
        if not tid:
            self.ctx.out.problem("there is no %r tag in this account yet; python src/agents/autopilot/curative_setup.py "
                                 "board makes it" % tag)
            return
        for row in self.ctx.crm.rows({"all_tags": [tid]}, cap=int(self.o["max_scan"])):
            if row.get("uuid"):
                self.rows.setdefault(row["uuid"], row)

    def read(self, u: str) -> dict | None:
        if self.reads >= int(self.o["max_records"]):
            return None
        try:
            rec = self.ctx.crm.record(u)
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read a record (%s)" % str(exc)[:100])
            return None
        self.reads += 1
        return rec if rec.get("uuid") else None

    def tree(self, u: str, rec: dict) -> dict | None:
        t = research_tree(u, rec, str(self.o["research_workflow"]))
        if t is None:
            t = loaded_tree(u, self.heir_dir)
        if t is not None:
            self.sources[t.get("source") or "the loader"] += 1
        return t

    # ---- writing the board
    def _save(self, u: str, f: dict | None) -> None:
        ledger.set_fact(u, self.key, f)
        if f is None:
            self.pos.pop(u, None)
        else:
            self.pos[u] = f

    def _first_holds(self, col_uuid: str, card: str, what: str) -> bool:
        """The first live card write of a run is read back; one that did not hold stops the writing."""
        if not self.ctx.live or self.verified:
            return True
        self.verified = True
        if leadroutes.card_in_column(self.ctx.crm, col_uuid, card):
            return True
        self.broken = True
        self.ctx.out.problem("the first curative card %s did not hold (it is not in its column after the write); nothing "
                             "more was written this run. Check the column ids under column_ids" % what)
        return False

    def create(self, u: str, owner: str, title: str, why: str) -> dict | None:
        ctx = self.ctx
        if self.broken:
            return None
        if self.creates >= int(self.o["max_creates"]):
            self.tally["waiting to be placed (the run's card limit; next run)"] += 1
            return None
        col = self.cols.get(title, "")
        card = "plan-%s" % u
        if ctx.live:
            ctx.change(u, "card_create", None, {"column": col, "board": self.board_uuid, "title": title, "kind": KIND})
            made = ctx.write("place card", leadroutes.create_card, ctx.crm, col, u) or {}
            card = str((made or {}).get("uuid") or "")
            if not card:
                card = next((str(c.get("uuid")) for c in leadroutes.cards(ctx.crm, col, cap=int(self.o["max_cards"]))
                             if leadroutes.card_prop(c) == u), "")
            if not card:
                self.broken = True
                ctx.out.problem("a curative card was sent to %s but its id could not be read back; nothing more was "
                                "written" % title)
                return None
            ledger.event(ctx.run_id, self.st.id, u, "card_create", {"card": card, "column": col, "title": title},
                         now=ctx.now, tz=ctx.policy.tz)
            if not self._first_holds(col, card, "placed"):
                return None
        else:
            ctx.write("place card", leadroutes.create_card, ctx.crm, col, u)
        self.creates += 1
        now_s = ctx.now.isoformat(timespec="seconds")
        f = {"card": card, "column": title, "column_uuid": col, "entered": now_s, "placed": now_s,
             "due": fp.due_day(ctx.now, self.zone, self.days, self.first_call).isoformat(), "moved_day": self.today_s,
             "owner": owner, "source": "placed", "why": why}
        self._save(u, f)
        self.placed[title] += 1
        return f

    def move(self, u: str, p: dict, title: str, why: str, advance: bool = False) -> dict | None:
        """Move a card. A move the round earned (`advance`) happens once a business day at most; a move
        that is not the round (to Heir Engaged, back to Research) is never held back by that."""
        ctx = self.ctx
        if self.broken or p.get("column") == title:
            return None
        if advance and p.get("moved_day") == self.today_s:
            self.tally["already moved today (one column a business day)"] += 1
            return None
        if self.moves >= int(self.o["max_moves"]):
            self.tally["moves left for the next run (the run's move limit)"] += 1
            return None
        frm, to = p.get("column_uuid") or self.cols.get(p.get("column") or "", ""), self.cols.get(title, "")
        if ctx.live:
            ctx.change(u, "card_move", {"card": p["card"], "column": frm, "board": self.board_uuid, "title": p["column"]},
                       {"card": p["card"], "column": to, "title": title})
            ctx.write("move card", leadroutes.move_card, ctx.crm, frm, p["card"], to)
            ledger.event(ctx.run_id, self.st.id, u, "card_move", {"from": p["column"], "to": title, "why": why},
                         now=ctx.now, tz=ctx.policy.tz)
            if not self._first_holds(to, p["card"], "moved"):
                return None
        else:
            ctx.write("move card", leadroutes.move_card, ctx.crm, frm, p.get("card") or "", to)
        self.moves += 1
        f = dict(p, column=title, column_uuid=to, entered=ctx.now.isoformat(timespec="seconds"),
                 due=fp.due_day(ctx.now, self.zone, self.days, self.first_call).isoformat(), moved_day=self.today_s,
                 why=why)
        self._save(u, f)
        self.moved["%s to %s" % (p.get("column"), title)] += 1
        return f

    def remove(self, u: str, p: dict, why: str) -> bool:
        ctx = self.ctx
        if self.broken:
            return False
        col = p.get("column_uuid") or self.cols.get(p.get("column") or "", "")
        if ctx.live:
            ctx.change(u, "card_remove", {"card": p["card"], "column": col, "board": self.board_uuid, "title": p["column"]}, None)
            ctx.write("take card off", leadroutes.remove_card, ctx.crm, col, p["card"])
            ledger.event(ctx.run_id, self.st.id, u, "card_remove", {"from": p["column"], "why": why},
                         now=ctx.now, tz=ctx.policy.tz)
        else:
            ctx.write("take card off", leadroutes.remove_card, ctx.crm, col, p.get("card") or "")
        self._save(u, None)
        self.off[why] += 1
        return True

    # ---- tasks
    def clear_tasks(self, u: str, p: dict, why: str, keep_today: bool = False) -> dict:
        """Take this stage's still-open day tasks off the record (all of them, or those from earlier days).
        A task the caller completed is left as it is: it holds the call's outcome. Returns the card fact
        with what is left."""
        ctx = self.ctx
        if not ctx.live:
            n = int(p.get("plan_open") or 0)
            if n and not (keep_today and p.get("tasks_day") == self.today_s):
                self.cleared += n
                p = dict(p, plan_open=0)
                self._save(u, p)
            return p
        left = []
        for t in list(p.get("tasks") or []):
            if keep_today and t.get("day") == self.today_s:
                left.append(t)
                continue
            try:
                state = researchroutes.task_open(ctx.crm, t["task"])
            except Exception as exc:  # noqa: BLE001  (an unread task stays on the list and is tried next run)
                ctx.out.problem("could not read a curative call task to clear it (%s)" % str(exc)[:100])
                left.append(t)
                continue
            if state:
                ctx.change(u, "task_delete", {"title": t.get("title") or "", "due": t.get("due") or ""}, None)
                ctx.write("clear call task", leadroutes.delete_task, ctx.crm, t["task"])
                ledger.event(ctx.run_id, self.st.id, u, "curative_task_cleared", {"cleared": t["task"], "why": why},
                             now=ctx.now, tz=ctx.policy.tz)
                self.cleared += 1
        if left != list(p.get("tasks") or []):
            p = dict(p, tasks=left)
            self._save(u, p)
        return p

    def day_tasks(self, u: str, p: dict, rec: dict, heirs: list, need: list) -> dict:
        """One call task per heir not yet reached, for the business day a card sits in Day N: written once
        that day, the day's open tasks from earlier days cleared first. A task gives the last four digits
        of the number to dial, never the whole number."""
        ctx, n = self.ctx, column_no(p.get("column") or "")
        if not 1 <= n <= DAYS or self.today.weekday() not in self.days:
            return p
        try:
            due = date.fromisoformat(p.get("due") or "")
        except ValueError:
            due = self.today
        if due > self.today or p.get("tasks_day") == self.today_s:
            return p
        if not self.caller:
            self.tally["call tasks not written: no curative caller"] += 1
            return p
        p = self.clear_tasks(u, p, "a new day of the round")
        owner = (rec.get("owner") or {}).get("uuid") or ""
        talked = self._reached(owner, heirs, p)
        todo = [(h, best) for h, best in need if h.key not in talked and (owner, best) not in self.tasked]
        if len(todo) < len([1 for h, _ in need if h.key not in talked]):
            self.tally["call tasks already written today on another house of the same owner"] += 1
        if not todo:
            if need:
                p = dict(p, tasks_day=self.today_s)
                self._save(u, p)
            return p
        if self.tasks + len(todo) > int(self.o["max_tasks"]):
            self.tally["call tasks left for the next run (the run's task limit)"] += 1
            return p
        nobody = [h for h in heirs if h.living is True and h.verified and h.key not in {x.key for x, _ in need}]
        unsure = [h for h in heirs if h.living is None]
        notes = METHOD + " The heir map is the PDF in this record's documents."
        if nobody:
            notes += " No number yet for %s: ask the family for one." % ", ".join(h.label() for h in nobody[:4])
        if unsure:
            notes += " Not confirmed living: %s. Ask the family before anything else." % ", ".join(h.label() for h in unsure[:4])
        street = street_of(rec)
        titles = ["Curative Day %d: call %s, number ending %s" % (n, h.label(), researchroutes.last4(best))
                  for h, best in todo]
        made = []
        if ctx.live:
            ctx.change(u, "task_create", None, {"titles": titles})
        for (h, best), title in zip(todo, titles):
            due_s = researchroutes.end_of_day(ctx.now, ctx.policy.tz)
            res = ctx.write("curative call task", researchroutes.create_task, ctx.crm, title, property_uuid=u,
                            user_uuid=self.caller["uuid"], due=due_s, notes=notes)
            self.tasks += 1
            self.tasked.add((owner, best))
            if ctx.live:
                tid = str((res or {}).get("uuid") or "") if isinstance(res, dict) else ""
                ledger.event(ctx.run_id, self.st.id, u, "curative_task", {"task": tid, "heir": h.key, "day": self.today_s,
                                                                         "column": p.get("column")},
                             now=ctx.now, tz=ctx.policy.tz)
                if tid:
                    made.append({"task": tid, "title": title, "due": due_s, "heir": h.key, "day": self.today_s})
        ctx.out.line("%s: %s, call %s" % (street, p.get("column"), "; ".join(h.label() for h, _ in todo[:3])
                                          + (" and %d more" % (len(todo) - 3) if len(todo) > 3 else "")))
        if ctx.live:
            p = dict(p, tasks=list(p.get("tasks") or []) + made, tasks_day=self.today_s)
        else:
            p = dict(p, tasks_day=self.today_s, plan_open=len(todo))
        self._save(u, p)
        return p

    def exhausted_task(self, u: str, p: dict, rec: dict, heirs: list, need: list) -> dict:
        """The one call task a card gets on reaching Exhausted, thirty days out (exhausted_callback_days)."""
        ctx = self.ctx
        if p.get("exhausted_task") or not self.caller:
            if not self.caller:
                self.tally["follow-up task not written: no curative caller"] += 1
            return p
        days = int(self.o["exhausted_callback_days"])
        first = next((h for h, _ in need), None) or next((h for h in heirs if h.living is True and h.verified), None)
        title = "Curative follow-up: call the heirs about %s again%s" % (
            street_of(rec), (", %s first" % first.label()) if first else "")
        due_s = researchroutes.end_of_day(ctx.now, ctx.policy.tz, days=days)
        if ctx.live:
            ctx.change(u, "task_create", None, {"titles": [title]})
        res = ctx.write("curative follow-up task", researchroutes.create_task, ctx.crm, title, property_uuid=u,
                        user_uuid=self.caller["uuid"], due=due_s,
                        notes=METHOD + " Five days of calls reached nobody; this is the follow-up %d days on." % days)
        self.tasks += 1
        tid = str((res or {}).get("uuid") or "") if ctx.live else "plan"
        if ctx.live:
            ledger.event(ctx.run_id, self.st.id, u, "curative_task", {"task": tid, "exhausted": True, "due": due_s},
                         now=ctx.now, tz=ctx.policy.tz)
        p = dict(p, exhausted_task=tid or "written")
        self._save(u, p)
        return p

    def _reached(self, owner: str, heirs: list, p: dict) -> set:
        """Heirs already talked with since the card was placed (an answered call of conversation_seconds)."""
        out = set()
        since = _when(p.get("placed")) or self.ctx.now
        for h in heirs:
            if engaging_call(owner, set(h.digits()), since, int(self.o["conversation_seconds"])):
                out.add(h.key)
        return out

    # ---- deciding
    def engage(self, u: str, p: dict, rec: dict, why: str, clear: bool = True) -> None:
        """An heir engaged, or the record became a lead: the card goes to Heir Engaged, the day tasks
        stop, staged letters come back out, and the team is told once. On a lead (`clear` False) the
        card move is the only write: the lead manager owns the record, its tasks included."""
        self.gone[u] = "an heir is talking with us"
        if clear:
            p = self.clear_tasks(u, p, "an heir engaged")
        if self.move(u, p, ENGAGED, why) is not None:
            self.ctx.out.alert(ALERT % street_of(rec))
            self.engaged.append(street_of(rec))

    def card(self, u: str, p: dict) -> None:
        n = column_no(p.get("column") or "")
        if n < 0:
            self.tally["cards in a column the board does not use (left alone)"] += 1
            return
        rec = self.read(u)
        if rec is None:
            self.tally["not judged this run (read cap; next run)"] += 1
            return
        row = self.rows.get(u)
        state, why = assess(self.ctx, rec, row, self.o)
        if state == "gone":
            self.gone[u] = why
            p = self.clear_tasks(u, p, why)
            self.remove(u, p, why)
            return
        if state == "lead":
            self.gone[u] = "the record is a lead"
            if n != ENGAGED_NO:
                self.engage(u, p, rec, why, clear=False)
            else:
                self.tally["in Heir Engaged: the caller has it"] += 1
            return
        if n == ENGAGED_NO:
            self.gone[u] = "an heir is talking with us"
            self.tally["in Heir Engaged: the caller has it"] += 1
            return
        if state == "hold":
            self.held[why] += 1
            return
        tree = self.tree(u, rec)
        heirs = heirs_of_tree(tree)
        owner = (rec.get("owner") or {}).get("uuid") or ""
        numbers = {n_ for h in heirs for n_ in h.digits()}
        since = _when(p.get("placed")) or self.ctx.now
        hit = engaging_call(owner, numbers, since, int(self.o["conversation_seconds"]))
        if hit:
            self.engage(u, p, rec, "an answered call of %d seconds on an heir's number" % int(hit["data"].get("duration") or 0))
            return
        if state == "exhausted":
            if n != EXHAUSTED_NO:
                p = self.clear_tasks(u, p, why)
                self.move(u, p, EXHAUSTED, why)
            else:
                self.tally["in Exhausted"] += 1
            return
        usable = fp.numbers(self.ctx, rec, row, "call", "heirs") if state == "work" else []
        need = need_of(heirs, usable)
        if state == "research":
            if n != 0:
                p = self.clear_tasks(u, p, why)
                self.move(u, p, RESEARCH, why)
            else:
                self.tally["in Research: %s" % why] += 1
            return
        wait = self._why_research(tree, need)
        if n == 0:
            if wait:
                self.tally["in Research: %s" % wait] += 1
                return
            # Ready is not started: the card waits in Ready for Marketing for the morning start.
            f = self.move(u, p, READY, "the heir tree has a verified heir with a number, and the PDF is on the record")
            if f:
                self.ready.append((u, f, rec, row, heirs, need))
            return
        if n == READY_NO:
            if wait:
                self.move(u, p, RESEARCH, wait)
                return
            self.ready.append((u, p, rec, row, heirs, need))
            return
        if n == EXHAUSTED_NO:
            self.tally["in Exhausted"] += 1
            self.owed.append((u, rec, row, heirs))
            return
        if wait:
            p = self.clear_tasks(u, p, wait)
            self.move(u, p, RESEARCH, wait)
            return
        self.day_card(u, p, rec, heirs, need)

    def _why_research(self, tree: dict | None, need: list) -> str:
        if tree is None:
            return "no heir tree on this box (the research, or the loader's heir file)"
        if not pdf_on(tree):
            return "the heir map (the PDF) is not on the record yet"
        if not need:
            return "no living verified heir has a number a caller may dial"
        return ""

    def day_card(self, u: str, p: dict, rec: dict, heirs: list, need: list) -> None:
        """A card on a day of the round. Before calls start the business days that are over are judged:
        the card moves the columns they earned (one a day at most) or holds, with the day it missed.
        Then the day's call tasks are written. After calls start, only what today still lacks is said."""
        n = column_no(p["column"])
        try:
            due = date.fromisoformat(p.get("due") or "")
        except ValueError:
            due = self.today
        owner = (rec.get("owner") or {}).get("uuid") or ""
        bad = {gate.digits(x.get("number")) for x in ((rec.get("owner") or {}).get("phones") or [])
               if str(x.get("status") or "UNKNOWN").upper() in gate.BAD_PHONE}
        start = datetime.combine(due, datetime.min.time(), tzinfo=self.zone)
        dialed = dialed_by_day(owner, start, self.zone, self.hours)
        if self.morning:
            until = self.today - timedelta(days=1)
            earned, misses = judge(n, due, until, lambda d: round_done(d, need, dialed, bad), self.days)
            if earned > n:
                last = max(d for d in dialed if due <= d <= until) if any(due <= d <= until for d in dialed) else until
                f = self.move(u, p, column_title(earned), "the round on %s" % _nice(last), advance=True)
                if f is not None:
                    p = f
                    if column_title(earned) == EXHAUSTED:
                        p = self.clear_tasks(u, p, "the round is over")
                        p = self.exhausted_task(u, p, rec, heirs, need)
                        self.owed.append((u, rec, None, heirs))
                        return
            elif misses:
                self.held["no call to every heir with a number on %s" % _nice(misses[-1])] += 1
            else:
                self.tally["due today"] += 1
        elif due <= self.today:
            got = dialed.get(self.today) or set()
            if round_done(self.today, need, dialed, bad):
                self.tally["today's round is in"] += 1
            else:
                left = sum(1 for h, best in need if best not in got)
                self.short["%s, %d heir(s) not called yet" % (p["column"], left)] += 1
        p = self.day_tasks(u, p, rec, heirs, need)
        self.owed.append((u, rec, None, heirs))

    def place_new(self) -> None:
        """Every tagged record with no card gets one: Ready for Marketing when it is ready, Research while it
        waits. Never Day 1: a house starts only by the morning start, so a run that places a hundred cards
        does not put a hundred families on the caller's list the same day."""
        o = self.o
        try:
            off_cut = (self.today - timedelta(days=int(o["off_days"]))).isoformat()
        except (TypeError, ValueError):
            off_cut = self.today_s
        for u, row in self.rows.items():
            if u in self.pos:
                continue
            off = ledger.fact(u, OFF_FACT)
            if isinstance(off, dict) and str(off.get("day") or "") >= off_cut:
                self.tally["taken off the board by a person (left off)"] += 1
                continue
            if status_of(row) in lanes.LEAD_STATUSES:
                self.tally["not placed: a lead, the lead board has it"] += 1
                continue
            if self.broken or self.creates >= int(o["max_creates"]):
                self.tally["waiting to be placed (the run's card limit; next run)"] += 1
                continue
            rec = self.read(u)
            if rec is None:
                self.tally["waiting to be placed (the run's read limit; next run)"] += 1
                continue
            state, why = assess(self.ctx, rec, row, o)
            if state in ("gone", "lead", "hold"):
                self.tally["not placed: %s" % why] += 1
                continue
            owner = (rec.get("owner") or {}).get("uuid") or ""
            if state == "exhausted":
                self.create(u, owner, EXHAUSTED, why)
                continue
            tree = self.tree(u, rec)
            heirs = heirs_of_tree(tree)
            need = need_of(heirs, fp.numbers(self.ctx, rec, row, "call", "heirs")) if state == "work" else []
            wait = why if state == "research" else self._why_research(tree, need)
            f = self.create(u, owner, RESEARCH if wait else READY, wait or "the heir tree is ready")
            if f is not None and not wait:
                self.ready.append((u, f, rec, row, heirs, need))

    # ---- the morning start
    def _starts_key(self) -> str:
        return STARTS_KV % ("live" if self.ctx.live else "plan", self.st.id, self.today_s)

    def starts(self) -> None:
        """Move at most daily_starts cards a business day from Ready for Marketing into Day 1, in the pass
        before calls start, the longest-waiting first (the card that entered Ready earliest). The move is
        the house's start: its Day 1 tasks are written and its letters fall due exactly as for any card on
        Day 1. The houses started each day are remembered, so a second morning pass never starts more."""
        if not self.ready:
            return
        cap = int(self.o["daily_starts"])
        if not self.morning or self.today.weekday() not in self.days:
            self.tally["in Ready for Marketing, waiting for the next morning's start"] += len(self.ready)
            return
        key = self._starts_key()
        try:
            done = [str(x) for x in json.loads(store.kv_get(key) or "[]")]
        except ValueError:
            done = []
        room = max(0, cap - len(done))
        line = sorted(self.ready, key=lambda x: (waited_since(x[1]), x[0]))
        for u, p, rec, row, heirs, need in line:
            if room <= 0 or self.broken:
                self.tally["in Ready for Marketing, waiting its turn (daily_starts %d)" % cap] += 1
                continue
            f = self.move(u, p, "Day 1", "started: the next house in line (Ready for Marketing since %s)"
                          % str(p.get("entered") or "")[:10])
            if f is None:
                continue
            f = dict(f, started=self.today_s)
            self._save(u, f)
            done.append(u)
            room -= 1
            self.started.append(street_of(rec))
            self.day_tasks(u, f, rec, heirs, need)
            self.owed.append((u, rec, row, heirs))
        store.kv_set(key, json.dumps(done))

    # ---- letters
    def letters(self) -> tuple[list, float]:
        """One letter per living verified heir with a complete address, owed from the day the card enters
        Day 1, staged for a person (never sent from here). The cap is daily_pieces; 0 while mail is held."""
        ctx, o, st = self.ctx, self.o, self.st
        if not self.owed:
            return [], 0.0
        bad_copy = audit_setting(o)
        if bad_copy:
            ctx.out.problem("the curative letter was refused: it %s. Nothing is staged until the copy is fixed." % bad_copy[0])
            return [], 0.0
        cap = int(o["daily_pieces"])
        vendor = mail.Vendor(dict(st.get("prices") or {}))
        sender = mail.sender_block(st)
        staged = heirlane.staged_keys(st.id, (KIND_MAIL,))
        body = letter_body(o)
        out, usd, spent = [], 0.0, False
        for u, rec, row, heirs in self.owed:
            if u in self.gone:
                continue
            sent = heirlane.letters_sent(u, st.id, SERIES)
            due = [h for h in heirs if h.living is True and h.verified and h.key not in sent
                   and (u, h.key, KIND_MAIL, 1) not in staged]
            if not due:
                continue
            vm = ctx.verdict(rec, "mail", row=row, allow=extra_allow(ctx, rec, o))
            if not vm.ok:
                self.tally["letters held back: %s" % vm.reason()] += len(due)
                continue
            street, city = street_of(rec), str((rec.get("address") or {}).get("city") or "")
            for h in due:
                if not h.mailable():
                    self.tally["heir with no full address (street, city, state and a 5-digit zip): no letter"] += 1
                    continue
                if cap <= 0:
                    self.tally["letters owed, held while mail is held (daily_pieces is 0)"] += 1
                    continue
                if len(out) >= cap:
                    self.tally["letters left for tomorrow (daily cap %d)" % cap] += 1
                    continue
                price = vendor.price(str(o["letter_piece"]))
                if spent:
                    self.tally["letters left for tomorrow (the day's mail budget is used)"] += 1
                    continue
                ok, why = ctx.budget(usd + price, u, record_usd=0)
                if not ok:
                    spent = True
                    self.tally["letters left for tomorrow (%s)" % why] += 1
                    continue
                text = render_letter(body, h.first, street, sender.get("name") or (self.caller or {}).get("name") or "",
                                     sender.get("phone") or "")
                bad = audit_letter(text, street)
                if bad:
                    ctx.out.problem("a curative letter for %s was refused: it %s" % (street, bad[0]))
                    continue
                usd += price
                out.append({"kind": "letter", "record_uuid": u, "heir": h.key, "name": h.name, "first_name": h.first,
                            "relationship": h.relationship, "piece": str(o["letter_piece"]), "letter": 1, "of": 1,
                            "street": h.street, "city": h.city, "state": h.state, "zip": h.zip,
                            "property_street": street, "property_city": city, "text": text, "price": price})
        return out, usd

    def pull(self) -> int:
        """Staged letters for a record that left the board, became a lead or has an heir talking come back
        out before release (the release checks them again besides)."""
        gone = dict(self.gone)
        whole = len(self.rows) < int(self.o["max_scan"])      # a capped read cannot say a record left the tag
        for b in ledger.batches(status="staged", stage=self.st.id):
            if b["kind"] != KIND_MAIL or not whole:
                continue
            for it in (ledger.batch(b["id"]) or {}).get("items") or []:
                u = it.get("record_uuid") or ""
                if u and u not in self.rows and u not in self.pos and u not in gone:
                    gone[u] = "no longer on the curative board"
        return heirlane.pull_staged(self.ctx, (KIND_MAIL,), gone) if gone else 0


def _when(stamp) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(stamp or ""))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_NEVER = datetime(9999, 1, 1, tzinfo=timezone.utc)


def waited_since(card: dict) -> datetime:
    """When a card entered its column, as a moment (so stamps written with different offsets still compare);
    a card with no stamp goes to the back of the line."""
    return _when((card or {}).get("entered")) or _NEVER


def _heirs_lane_marked(ctx: Ctx, o: dict) -> bool:
    cfg = getattr(ctx.policy, "lanes", None) or lanes.Lanes()
    m = (getattr(cfg, "markers", None) or {}).get("heirs") or {}
    return _tag_of(o) in {str(t).strip().lower() for t in (m.get("tags") or []) + (m.get("lists") or [])}


def run_curative(ctx: Ctx) -> None:
    o = options(ctx.stage)
    r = Run(ctx, o)
    if r.today.weekday() not in r.days:
        ctx.out.head = "%s is not a business day; the curative board waits for the next one." % r.today.strftime("%A")
        return
    if not _heirs_lane_marked(ctx, o):
        ctx.out.problem("the playbook's heirs lane does not carry the tag %s: set `heirs` under lanes to [Heir Identified, "
                        "%s], so the family's other records are held while the board works them" % (o["tag"], o["tag"]))
    who = o["caller"]
    people = resolve_callers(ctx, [who]) if who else resolve_callers(ctx)
    r.caller = people[0] if people else None
    if not who and r.caller:
        ctx.out.line("No curative caller is named in the playbook; %s gets the call tasks." % r.caller["name"])
    if not r.caller:
        ctx.out.problem("the curative caller is not named in the playbook (caller), so no call task is written")
    r.sync()
    if not r.load_board():
        return
    r.read_cards()
    r.read_rows()
    ctx.out.found = len(set(r.pos) | set(r.rows))
    # The days of the round first: they are judged on a clock, and the run's read cap must never be spent on
    # the cards that are waiting (Research) or done (Heir Engaged, Exhausted) before them. Then Ready for
    # Marketing, the longest-waiting first: those are the next to start, so the cap never cuts the head of
    # the line.
    def rank(kv):
        n = column_no(kv[1].get("column") or "")
        if 1 <= n <= DAYS:
            return (0, n, _EPOCH, kv[0])
        if n == READY_NO:
            return (1, 0, waited_since(kv[1]), kv[0])
        return (2, n, _EPOCH, kv[0])
    order = sorted(r.pos.items(), key=rank)
    for u, p in order:
        r.card(u, p)
    r.place_new()
    r.starts()
    letters, usd = r.letters()
    pulled = r.pull()
    batch = 0
    if letters and ctx.live:
        batch = ledger.batch_create(r.st.id, KIND_MAIL, letters, usd)
        heirlane.export(batch, KIND_MAIL, letters)
        ctx.out.batch = batch
    elif letters:
        ctx.reserve(usd)
    _report(ctx, r, letters, usd, pulled, batch)


def _report(ctx: Ctx, r: Run, letters: list, usd: float, pulled: int, batch: int) -> None:
    now_cols = Counter(p.get("column") for p in r.pos.values())
    board = ", ".join("%s %d" % (t, now_cols[t]) for t in COLUMNS if now_cols.get(t))
    live = ctx.live
    parts = []
    if r.placed:
        parts.append("%s %d card(s) (%s)" % ("placed" if live else "place", sum(r.placed.values()),
                                             ", ".join("%s %d" % (t, r.placed[t]) for t in COLUMNS if r.placed.get(t))))
    moved = Counter(r.moved)
    start_key = "%s to Day 1" % READY
    moved[start_key] -= len(r.started)          # a start is said on its own, not as one more move
    if moved[start_key] <= 0:
        del moved[start_key]
    if r.started:
        parts.append("%s %d house(s) into Day 1 (daily_starts %d)" % ("started" if live else "start", len(r.started),
                                                                     int(r.o["daily_starts"])))
    if moved:
        parts.append("%s %d (%s)" % ("moved" if live else "move", sum(moved.values()),
                                     ", ".join("%d from %s" % (n, k) for k, n in sorted(moved.items()))))
    if r.off:
        parts.append("%s %d off the board (%s)" % ("took" if live else "take", sum(r.off.values()),
                                                   ", ".join("%s: %d" % kv for kv in r.off.most_common(3))))
    if r.tasks:
        parts.append("%s %d call task(s) for %s" % ("wrote" if live else "write", r.tasks,
                                                   (r.caller or {}).get("name") or "the caller"))
    if r.cleared:
        parts.append("%s %d earlier call task(s)" % ("cleared" if live else "clear", r.cleared))
    if letters:
        parts.append("%s %d letter(s), about $%.2f" % ("staged" if live else "stage", len(letters), usd))
    if pulled:
        parts.append("%d staged letter(s) pulled" % pulled)
    ctx.out.acted = (sum(r.placed.values()) + sum(r.moved.values()) + sum(r.off.values()) + r.tasks + r.cleared
                     + len(letters) + pulled)
    pass_word = "before calls" if r.morning else "after calls started"
    if parts:
        text = "; ".join(parts) + "."
        head = text[0].upper() + text[1:] if live else "Would " + text
    else:
        head = "Nothing to place or move on the curative board (%s)." % pass_word
    if r.held:
        head += " Held %d: %s." % (sum(r.held.values()), ", ".join("%s (%d)" % kv for kv in r.held.most_common(3)))
    if r.short:
        head += " Short today: %s." % ", ".join("%s (%d)" % kv for kv in r.short.most_common(3))
    if batch:
        head += " Staged for a person: letters batch %d (python src/agents/cli.py autopilot release %d)." % (batch, batch)
    ctx.out.head = head
    if board:
        ctx.out.line("On the curative board: %s." % board)
    if r.started:
        ctx.out.line("Started today: %s%s." % ("; ".join(r.started[:5]),
                                               " and %d more" % (len(r.started) - 5) if len(r.started) > 5 else ""))
    for street in r.engaged[:3]:
        ctx.out.line("%s: an heir is talking with us, the day tasks stopped." % street)
    if r.sources:
        ctx.out.line("Heir trees from %s." % ", ".join("%s %d" % (k, v) for k, v in r.sources.most_common()))


# ---------------------------------------------------------------------- releasing the letters
def still_due(ctx: Ctx, rec: dict, it: dict) -> str:
    """Why a staged curative letter may no longer go out, or ''. Run on every letter at release."""
    if not rec.get("uuid"):
        return "the record could not be read"
    o = options(ctx.stage)
    if _tag_of(o) not in marks_of(rec):
        return "the record is no longer tagged %s" % o["tag"]
    if status_of(rec) in lanes.LEAD_STATUSES:
        return "the record is a lead now: the lead manager has it"
    p = position(rec["uuid"])
    if p and p.get("column") == ENGAGED:
        return "an heir is talking with us, so no letter goes while that goes on"
    others = other_research_tags(ctx, rec, o)
    if others:
        return "held for research (tag %s)" % others[0]
    v = ctx.verdict(rec, "mail", allow=extra_allow(ctx, rec, o))
    if not v.ok:
        return v.reason()
    return ""


def record_piece(ctx: Ctx, rec: dict, it: dict, ref: str, state: dict) -> None:
    """A released curative letter, written down: in the ledger under the heir it went to (the roster,
    first), then on the record (the month tag, the last-mailed date and a note)."""
    u = it["record_uuid"]
    today = ledger.day(ctx.now, ctx.policy.tz)
    cost = float(it.get("price") or 0)
    ledger.event(ctx.run_id, ctx.stage.id, u, "mail",
                 {"piece": it.get("piece"), "step": 1, "vendor": "csv", "ref": ref, "logged": "tag+note",
                  "series": SERIES, "heir": it.get("heir"), "heir_name": it.get("name")},
                 usd=round(cost, 4), now=ctx.now, tz=ctx.policy.tz)
    ctx.settle(0, cost, "curative letter")
    o = options(ctx.stage)
    try:
        tag = str(o.get("mailed_tag") or "").format(yyyy=today[:4], mm=today[5:7])
        if tag:
            ctx.crm.add_tags(u, [tag], rec or None)
        if "field" not in state:
            try:
                state["field"] = ctx.crm.custom_field_uuid(str(o.get("last_mailed_field") or ""))
            except Exception:  # noqa: BLE001
                state["field"] = ""
        if state.get("field"):
            ctx.crm.set_custom_field(u, state["field"], today)
        ctx.crm.message(u, "Mailed: curative letter to %s%s at their own address, %s." % (
            it.get("name") or "an heir", (" (%s)" % it["relationship"]) if it.get("relationship") else "", ref), pin=False)
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("the curative letter to an heir of %s is in the ledger, but the record was not marked (%s)" % (
            it.get("property_street") or "a record", str(exc)[:80]))


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Release a batch of curative letters: a file a person uploads, so two steps and the gate at both
    (heirlane.release_roster)."""
    return heirlane.release_roster(batch_id, (KIND_MAIL,), still_due, record_piece, crm=crm, pol=pol, commit=commit,
                                   uploaded=uploaded, now=now)
