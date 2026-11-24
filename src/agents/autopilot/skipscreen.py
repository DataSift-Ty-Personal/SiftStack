"""The SmartSkip screen (Ty, 2026-11-23): every first-to-market record traced once through SmartSkip.

Ty: "we probably should smart-skip every first-to-market record. As part of the deep prospecting process ...
we can automatically know when there's a deceased record on file." And on probates: the mail has to reach the
person who decides, not the dead person's house.

WHY. Three things went wrong without it.
  * A death on file was found only when research reached the record, days or weeks after the texts and the
    calls started. SmartSkip lists a death on about one owner in seven of the first-to-market records it was
    measured on (4 of 27 foreclosure research runs), and a Deceased flag on the OWNER OF RECORD is treated as a
    death (Ty, 2026-11-19). The retrace bought SmartSkip and threw that flag away.
  * A probate's owner of record is the court's personal representative, and the upload set that person's
    mailing address to the house on 65% of probate rows (the parser took the first address after the role
    word, sometimes the attorney's). So probate mail went to the dead person's house. SmartSkip, asked about
    the DECEDENT at the house, lists the family with where each of them lives: the representative is one of
    them, with their own address and numbers.
  * Research and the retrace each bought SmartSkip for the same records. This stage writes the ledger's trace
    row, so the retrace never buys SmartSkip again for a record traced here (and this stage never buys for a
    record the retrace or any other source traced), and research reuses the answer for 30 days: it reads the
    fact FACT ("skip_screen") in dream100_dp.phase_smartskip, by its keys role, first, last, at and parsed.

WHO IS TRACED.
  * A foreclosure or an obituary record: the owner of record. Their mailing address is SmartSkip's required
    mailing field; the house goes in the property fields.
  * A probate record (the Probate list, or the probate tag) whose Decedent Name field reads as one person: the
    DECEDENT at the house. The representative is normally the owner of record, and is found among the
    relatives SmartSkip lists (find_pr): the same first and last name, or a first initial or a nickname of the
    first name with the same last name; two candidates means none.
  * A probate record with no readable Decedent Name: the owner of record (the representative), numbers only.
  People only: a company or a trust is never traced here (deep_prospect_batch.is_entity).

WHAT IS WRITTEN, each through ctx.write and each recorded with the value it replaced (`autopilot undo <run>`):
  * The owner of record listed as deceased, on a record that is not a probate: the owner's deceased mark, the
    death tag swap (research.writeback.death_tags: deceased on, living and Verify Deceased off), a fact naming
    the order, and one note. Never on a living namesake (death_verdict). This stage never queues research.
  * On a probate, a death SmartSkip lists on the owner of record (the representative, or a decedent who is
    still the owner) is a hold, never the settled marks: the Verify Deceased tag and a note.
  * A living subject's own numbers, scored and landed exactly as the retrace lands them (stages._write_numbers:
    Dial First and Second, Third only when nothing better, at most keep_numbers, Drop marked dead, the line
    type, the skip trace counter). A relative's numbers never land on a living owner.
  * A probate representative found with a complete address (street, city, two-letter state, five-digit ZIP):
    their numbers land on the owner (the representative) tagged Personal Representative too; the owner's mailing
    address becomes theirs when it is blank or the house (an owner's own address is safe to set on an owner
    object several records share; renaming one is not); the Decision Maker and DM Relationship fields are filled
    when empty. A representative who lives at the house keeps it, and is counted. A mailing address that is a
    third address is left alone and listed for a person.
  * Every traced record: the ledger's trace row (source smartskip, the order id) and a fact with what SmartSkip
    said (the flag, the relatives, whether the representative was found, the order), for research.

MONEY AND ORDERS. One SmartSkip order a run, of at most max_per_order records, oldest landing first, inside the
stage, lane, autopilot and brain caps and the per-record cap; 15 cents a record that returns anything, then
the scoring. An order is in SmartSkip's order book (sources.SmartSkip) before the card is charged, and a run
first collects any order an earlier run paid for and could not finish, BEFORE it orders anything new. While
an earlier order is still running, or still waits for a card confirmation, nothing new is ordered.

THE BACKFILL. `python -m agents.autopilot.skipscreen paid-orders [--commit]` reads every order SmartSkip
already finished on this account (re-downloading one is free), and puts on the record each death SmartSkip
listed on an owner of record, with the same rule and the same refusals. It never buys anything, and writes
only with --commit AND SKIPSCREEN_ALLOW=1.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parents[2]
_HERE = str(Path(__file__).resolve().parent)
if __name__ == "__main__":
    # Run as a script, this folder would shadow modules of the same name (mail, policy). Only src belongs there.
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _HERE]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import config, store  # noqa: E402
from agents.autopilot import activity, gate, lanes, ledger  # noqa: E402
from agents.autopilot.phonecleanup import NICKNAMES, person_names  # noqa: E402
from agents.autopilot.sources import SmartSkip, Subject, digits, same_street, subject_of  # noqa: E402
from agents.autopilot.stages import (DEAD_STATUSES, REFRESH_ACTION, TIER_NAMES, Ctx, Summary, _list,  # noqa: E402
                                     _mark_agreed, _write_numbers, addr, marks_of, phone_payload, phone_tier,
                                     stage_rows, status_of, tag_titles)
from agents.research import writeback as wb  # noqa: E402

log = logging.getLogger(__name__)

KIND = "skipscreen"
SOURCE = "smartskip"
SOURCE_TAG = "SmartSkip"                 # the tag stages._write_numbers puts on a SmartSkip number
FACT = "skip_screen"                     # what SmartSkip said about a record this stage traced (live runs only)
DEATH_FACT = "skip_trace_death"          # the order that settled an owner's death (live runs only)
SKIP_FACT = "skip_screen_skip"           # why a record was passed over for a lasting reason (kept in plan mode too)
PERSON_FACT = "skip_screen_person"       # a probate whose mailing address waits on a person (live runs only)
UNDOABLE = ("owner_deceased", "owner_address")
ALLOW_ENV = "SKIPSCREEN_ALLOW"
RECENT_DAYS = 90                        # how far back the shared log copy is read to put the oldest records first
DEFAULT_STAGE = "ftm_skip_screen"

DEATH_NOTE = "SmartSkip lists %s as deceased. Research is next; do not call or text the owner."
VERIFY_NOTE = ("SmartSkip lists %s, the owner of record on this probate, as deceased. Not confirmed: hold all "
               "outreach until research says who died.")
LOGIN_PROBLEM = ("SmartSkip's login has not been set up on this box (SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD), "
                 "so nothing was traced")
CARD_PROBLEM = ("a SmartSkip order needs a person to confirm the card payment in the SmartSkip app, so no new "
                "order was placed")
PERSON_PROBLEM = ("some probate records already mail to a third address, neither the house nor the "
                  "representative's own; the list is on the box for a person to decide")
ENTITY_WHY = "a company or a trust owns it: people only"
NO_PR = "the representative is not among the relatives SmartSkip lists"
TWO_PR = "two relatives could be the representative, so neither was used"

DEFAULTS = {
    "max_per_order": 100,           # records in one SmartSkip order (one order a run)
    "max_reads": 150,               # records opened a run to pick the order
    "max_scan": 2000,               # rows read from one preset
    "recheck_days": 14,             # a record passed over for a lasting reason is read again after this
    # trace: a record held only for a POSSIBLE death (Verify Deceased, the Obituary list) is traced, because
    # knowing is the point; hold: leave those to research. A settled death is never traced either way.
    "possible_death": "trace",
    "probate_marks": ["Probate"],   # a list or tag (any case) that makes a record a probate
    "decedent_field": "Decedent Name",
    "pr_field": "Personal Representative",
    "dm_field": "Decision Maker",
    "dm_relationship_field": "DM Relationship",
    "pr_relationship": "Personal Representative",
    "pr_tag": "Personal Representative",
    "verify_tag": "Verify Deceased",
    # Research already has the record: it bought or will buy SmartSkip itself.
    "skip_tags": ["Deep Prospected", "Heir Identified", "Heirs Resolved", "Research Queued"],
    "max_wait_s": 600,              # how long this run waits for its own order
    "collect_wait_s": 0,            # how long it waits for an order an earlier run left (0: one look)
    "card_wait_days": 3,            # an order whose payment was never confirmed is given up after this
    "record_usd": None,             # lifetime trace and scoring spend on one record; default the playbook's caps.record_usd
}
POSSIBLE_DEATH = ("trace", "hold")
INT_RANGES = {"max_per_order": (1, 1000), "max_reads": (1, 5000), "max_scan": (1, 10000)}

_GEN = {"jr": "jr", "junior": "jr", "sr": "sr", "senior": "sr", "ii": "ii", "iii": "iii", "iv": "iv"}
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "md", "esq", "rev"}
_PARTICLES = {"de", "del", "della", "der", "di", "da", "la", "le", "van", "von", "st", "ste", "dos", "das"}
DEATH_WORDS = re.compile(r"\b(?:the\s+)?estate\s+of\b|\bde-?\s*ceased\b|\bdecedent\b|\bthe\s+late\b", re.I)
AKA = re.compile(r"\b(?:a\s*/?\s*k\s*/?\s*a|f\s*/?\s*k\s*/?\s*a|also\s+known\s+as|formerly\s+known\s+as)\b", re.I)
NOT_A_PERSON = re.compile(r"\b(?:court|chancery|probate|clerk|attorneys?|law|firm|pllc|llc|inc|corp|company|bank|"
                          r"county|unknown|heirs?|trusts?|trustees?|none|pending)\b", re.I)


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k) if hasattr(st, "get") else None
        o[k] = v if got is None else got
    return o


def _strings(v) -> bool:
    return isinstance(v, (list, tuple)) and all(isinstance(x, str) and x.strip() for x in v)


def block_problems(st) -> list[str]:
    """What is wrong with the stage's block, read when the playbook loads, so a typo is refused instead of read
    as a default. A phone tag that is a tier would put the representative's numbers in two dial lanes at once."""
    o = settings(st)
    p = []
    for k, (least, most) in INT_RANGES.items():
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, int) or not least <= v <= most:
            p.append("stage %s: %s must be a whole number from %d to %d" % (st.id, k, least, most))
    for k in ("recheck_days", "max_wait_s", "collect_wait_s", "card_wait_days"):
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            p.append("stage %s: %s must be a number of at least 0" % (st.id, k))
    if o["possible_death"] not in POSSIBLE_DEATH:
        p.append("stage %s: possible_death must be trace or hold" % st.id)
    for k in ("probate_marks", "skip_tags"):
        if not isinstance(o[k], (list, tuple)) or (o[k] and not _strings(o[k])):
            p.append("stage %s: %s must be a list of names" % (st.id, k))
    if not o["probate_marks"]:
        p.append("stage %s: probate_marks must name at least one list or tag" % st.id)
    for k in ("decedent_field", "pr_field", "dm_field", "dm_relationship_field", "pr_relationship", "pr_tag",
              "verify_tag"):
        if not isinstance(o[k], str) or not o[k].strip():
            p.append("stage %s: %s must be a name" % (st.id, k))
    if isinstance(o["pr_tag"], str) and o["pr_tag"].strip() in TIER_NAMES:
        p.append("stage %s: pr_tag %r is a tier; it must be its own tag" % (st.id, o["pr_tag"]))
    for k in ("write_tiers", "reserve_tiers"):
        bad = [t for t in _list(st.get(k)) if t not in TIER_NAMES]
        if bad:
            p.append("stage %s: %s has %s (tiers are %s)" % (st.id, k, bad, ", ".join(TIER_NAMES)))
    v = o["record_usd"]
    if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
        p.append("stage %s: record_usd must be a positive number of dollars" % st.id)
    return p


# ---------------------------------------------------------------------- names
def _title(w: str) -> str:
    return w.title() if (w.isupper() or w.islower()) else w


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z']*", text or "")


def person_name(value) -> tuple[str, str]:
    """(first, last) of the one person a free-text name field names, or ('', '') when it names nobody (a court,
    an estate, a firm, "Unknown") or cannot be read as a first and a last name. "Estate of Pat Q. Example",
    "EXAMPLE, PAT Q" and "Pat Q Example, Deceased" all read as Pat Example. The first of two people is taken
    ("Pat and Robin Example"); an initial is never sent as a first name, because a trace on half a name bills
    for a stranger."""
    s = wb.plain(value)
    if not s or re.search(r"\d", s):
        return "", ""
    s = AKA.split(s)[0]
    s = DEATH_WORDS.sub(" ", s)
    if NOT_A_PERSON.search(s):
        return "", ""
    parts = [x.strip(" ,;.") for x in re.split(r"\s*(?:&|;|/|\+|\band\b)\s*", s, flags=re.I) if x.strip(" ,;.")]
    if not parts:
        return "", ""
    one = parts[0]
    if len(_words(one.replace(",", " "))) == 1 and len(parts) > 1:
        tail = [w for w in _words(parts[-1].replace(",", " ")) if w.lower() not in _GEN and w.lower() not in _TITLES]
        if tail:
            one = "%s %s" % (one, tail[-1])
    if "," in one:
        last, rest = one.split(",", 1)
        ws = _words(rest) + _words(last)
    else:
        ws = _words(one)
    ws = [w for w in ws if w.lower() not in _GEN and w.lower() not in _TITLES]
    while ws and len(ws[-1]) == 1:
        ws.pop()
    if len(ws) < 2:
        return "", ""
    i = len(ws) - 1
    while i - 1 >= 1 and ws[i - 1].lower() in _PARTICLES:
        i -= 1
    given = [w for w in ws[:i] if len(w) > 1]
    if not given:
        return "", ""
    return _title(given[0]), " ".join(_title(w) for w in ws[i:])


def _norm_first(s) -> str:
    toks = [t for t in re.sub(r"[^a-z ]", " ", str(s or "").lower()).split() if t not in _GEN and t not in _TITLES]
    return toks[0] if toks else ""


def _norm_last(s) -> str:
    toks = [t for t in re.sub(r"[^a-z ]", " ", str(s or "").lower().replace("-", " ")).split()
            if t not in _GEN and t not in _TITLES]
    return "".join(toks)


def firsts_agree(a, b, initials: bool = False) -> bool:
    """The same first name, or a nickname of it (Pat and Patricia, Bill and William). With `initials`, also a
    bare initial on either side that is the other's first letter. A shared first letter alone is never enough:
    it would read a brother Robert as his sister Rachel."""
    a, b = _norm_first(a), _norm_first(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if initials and (len(a) == 1 or len(b) == 1):
        return a[0] == b[0]
    return any(a in g and b in g for g in NICKNAMES)


def same_person(first_a, last_a, first_b, last_b, initials: bool = False) -> bool:
    la = _norm_last(last_a)
    return bool(la) and la == _norm_last(last_b) and firsts_agree(first_a, first_b, initials)


def generation(*parts) -> str:
    for t in re.sub(r"[^a-z ]", " ", " ".join(str(p or "") for p in parts).lower()).split():
        if t in _GEN:
            return _GEN[t]
    return ""


def full_name(first, last) -> str:
    return " ".join(_title(w) for w in ("%s %s" % (first or "", last or "")).split())


def find_pr(entry: dict, first: str, last: str) -> tuple[dict | None, str]:
    """(the relative SmartSkip lists who is the record's owner of record, why none). The representative is
    normally the owner of record, so this is the owner's name among the decedent's relatives: the same first and
    last name, a nickname, or a bare initial with the same last name. Two different candidates means none: the
    wrong one would put a stranger's numbers on the owner and send the probate's mail to the stranger. One
    person listed twice at the same street is one candidate."""
    found: list[dict] = []
    for r in (entry or {}).get("relatives") or []:
        if not isinstance(r, dict) or not same_person(r.get("first"), r.get("last"), first, last, initials=True):
            continue
        twin = next((c for c in found if _norm_first(c.get("first")) == _norm_first(r.get("first"))
                     and _norm_last(c.get("last")) == _norm_last(r.get("last"))
                     and (same_street(c.get("mailing_street") or "", r.get("mailing_street") or "")
                          or (c.get("mailing_street") or "").strip().lower() == (r.get("mailing_street") or "").strip().lower())),
                    None)
        if twin is None:
            found.append(r)
    if len(found) == 1:
        return found[0], ""
    return None, (NO_PR if not found else TWO_PR)


# ---------------------------------------------------------------------- addresses
def _zip5(z) -> str:
    d = re.sub(r"\D", "", str(z or ""))
    return d[:5] if len(d) >= 5 else ""


def _place(street, city, state, zipc) -> dict:
    return {"street": " ".join(str(street or "").split()), "city": " ".join(str(city or "").split()),
            "state": str(state or "").strip().upper(), "zip": _zip5(zipc)}


def place_of(rec: dict) -> dict:
    """The house."""
    a = (rec or {}).get("address") or {}
    return _place(a.get("street"), a.get("city"), a.get("state"), a.get("postal_code") or a.get("zip5"))


def mailing_of(owner: dict) -> dict:
    a = (owner or {}).get("address") or {}
    return _place(a.get("street"), a.get("city"), a.get("state"), a.get("postal_code") or a.get("zip5"))


def relative_place(r: dict) -> dict:
    return _place(r.get("mailing_street"), r.get("mailing_city"), r.get("mailing_state"), r.get("mailing_zip"))


def complete(a: dict) -> bool:
    """Street, city, a two-letter state and a five-digit ZIP: a letter can be addressed to it."""
    return bool(a.get("street") and a.get("city") and re.fullmatch(r"[A-Z]{2}", a.get("state") or "")
                and len(a.get("zip") or "") == 5)


def same_place(a: dict, b: dict) -> bool:
    if not (a.get("street") and b.get("street")) or not same_street(a["street"], b["street"]):
        return False
    za, zb = a.get("zip") or "", b.get("zip") or ""
    return not za or not zb or za == zb


def _field(custom: dict | None, label: str):
    want = (label or "").strip().lower()
    for k, v in (custom or {}).items():
        if str(k).strip().lower() == want:
            return v
    return None


def _when(stamp):
    try:
        at = datetime.fromisoformat(str(stamp or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------- the death rule (the stage and the backfill)
def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return is_entity(owner or {})


def _lead(rec: dict) -> bool:
    """A lead here, or the owner is a lead on another record: the lead manager has it."""
    if lanes.status_of(rec) in lanes.LEAD_STATUSES:
        return True
    ow = lanes.owner_lane(((rec or {}).get("owner") or {}).get("uuid") or "")
    return bool(ow and ow.get("lane") == "lead")


def touch_ok(ctx: Ctx, rec: dict) -> tuple[bool, str]:
    """May this stage write a mark or a note on the record? Never on a lead, and never where the gate's
    research channel holds it (Do Not Market, a litigator, an opt-out, a sold house, a finished status)."""
    if _lead(rec):
        return False, "a lead: the lead manager has it"
    v = ctx.verdict(rec, "research")
    return (True, "") if v.ok else (False, v.reason())


def trace_ok(ctx: Ctx, rec: dict, o: dict, row: dict | None = None) -> tuple[bool, str]:
    """May numbers land, or the mailing address change? The gate's trace channel, with a possible death lifted
    when the stage traces possible deaths."""
    allow = {"deceased"} if o["possible_death"] == "trace" else set()
    v = ctx.verdict(rec, "trace", row=row, allow=allow)
    return (True, "") if v.ok else (False, v.reason())


def death_verdict(ctx: Ctx, rec: dict, person: dict, o: dict, relatives=()) -> tuple[str, str]:
    """(settle | verify | already | refuse, why) for a SmartSkip Deceased flag on `person` ({first, last,
    property_address}) against the record's owner of record. The same rule for the stage and the backfill:

      * people only, and the person must BE the owner of record: the same last name and first name (or a
        nickname of it). A shared first letter is not enough: a husband on the title beside the owner is
        another person with the same initial and the same surname;
      * a senior and a junior are two people (research.writeback.deceased_flag's own guard);
      * a living relative SmartSkip lists under the owner's name is a namesake: the death may be the father's
        and the owner of record the son, so the mark is refused;
      * a lead is never touched, nor a record the gate's research channel holds;
      * an owner already marked deceased is left as it is;
      * on a probate the owner of record is the court's representative, so the flag is a hold (verify), never
        the settled marks."""
    ow = (rec or {}).get("owner") or {}
    if _is_entity(ow):
        return "refuse", ENTITY_WHY
    if not same_person(person.get("first"), person.get("last"), ow.get("first_name"), ow.get("last_name")):
        return "refuse", "the person SmartSkip lists is not the owner of record by name"
    ga = generation(ow.get("first_name"), ow.get("last_name"))
    gb = generation(person.get("first"), person.get("last"), person.get("name"))
    if ga and gb and ga != gb:
        return "refuse", "a senior and a junior share the name"
    prop, street = str(person.get("property_address") or ""), str(((rec or {}).get("address") or {}).get("street") or "")
    if prop and street and not same_street(street, prop):
        return "refuse", "SmartSkip's answer is for another house"
    label = wb.owner_label(ow)
    for r in relatives or ():
        if not isinstance(r, dict) or r.get("deceased"):
            continue
        # Wide on purpose: the write-back's name test, or a nickname of the first name ("Pat" beside
        # "Patricia"). Two people of one name in one family make the flag's owner unknowable.
        if (wb.same_name(full_name(r.get("first"), r.get("last")), label)
                or same_person(r.get("first"), r.get("last"), ow.get("first_name"), ow.get("last_name"))):
            return "refuse", "a living relative shares the owner's name"
    ok, why = touch_ok(ctx, rec)
    if not ok:
        return "refuse", why
    if ow.get("deceased"):
        return "already", "the owner is already marked deceased"
    if marks_of(rec) & {m.strip().lower() for m in o["probate_marks"]}:
        return "verify", "a probate: the owner of record is the court's representative"
    return "settle", ""


def _run_state() -> dict:
    return {"death_checked": False, "stop_death": False, "address_checked": False, "stop_address": False,
            "owners_marked": set()}


def settle_death(ctx: Ctx, rec: dict, person: dict, order: str, state: dict, by: str = "skip screen") -> str:
    """The owner's deceased mark, the death tag swap, the fact naming the order and one note. The first mark of a
    run is read back; a mark that did not stick stops the marks for the rest of the run."""
    u, ow = rec["uuid"], rec.get("owner") or {}
    name = full_name(person.get("first"), person.get("last"))
    if state["stop_death"]:
        ctx.out.tally["deaths not marked: an earlier mark this run did not stick"] += 1
        return "not marked: an earlier mark this run did not stick"
    owner_uuid = ow.get("uuid") or ""
    if owner_uuid and owner_uuid not in state["owners_marked"]:
        ctx.change(u, "owner_deceased", {"owner_uuid": owner_uuid, "deceased": bool(ow.get("deceased"))},
                   {"owner_uuid": owner_uuid, "deceased": True})
        ctx.write("owner deceased", ctx.crm.update_owner, owner_uuid, {"deceased": True})
        state["owners_marked"].add(owner_uuid)
        if ctx.live and not state["death_checked"]:
            state["death_checked"] = True
            try:
                back = ctx.crm.owner(owner_uuid) or {}
            except Exception:  # noqa: BLE001  (an unreadable read-back is a mark not proven)
                back = {}
            if back.get("deceased") is not True:
                state["stop_death"] = True
                ctx.out.problem("the owner's deceased mark did not stick on %s, so no more deaths were marked this run"
                                % addr(rec))
                return "the deceased mark did not stick"
    add, drop = wb.death_tags(rec.get("tags"))
    if add:
        ctx.change(u, "add_tags", None, add)
        ctx.write("death tag", ctx.crm.add_tags, u, add, rec)
    if drop:
        ctx.change(u, "remove_tags", drop, None)
        ctx.write("unsettled tags off", ctx.crm.remove_tags, u, drop)
    ctx.write("death note", ctx.crm.message, u, DEATH_NOTE % name)
    if ctx.live:
        ledger.set_fact(u, DEATH_FACT, {"order": order, "at": ctx.now.isoformat(timespec="seconds"), "name": name,
                                        "by": by})
        ledger.event(ctx.run_id, ctx.stage.id, u, "skip_death", {"order": order, "by": by}, now=ctx.now,
                     tz=ctx.policy.tz)
    ctx.out.tally["owners SmartSkip lists as deceased, %s" % ("marked" if ctx.live else "to mark")] += 1
    ctx.out.line("%s: SmartSkip lists the owner as deceased; %s" % (
        addr(rec), "marked, research is next" if ctx.live else "would be marked"))
    return "marked deceased" if ctx.live else "would mark deceased"


def verify_death(ctx: Ctx, rec: dict, person: dict, o: dict, order: str, by: str = "skip screen") -> str:
    """A probate's owner of record listed as deceased: the Verify Deceased tag and a note, never the settled
    marks. The court named that person to act for the estate; one vendor's flag does not undo that."""
    u = rec["uuid"]
    tag = o["verify_tag"]
    if tag.strip().lower() in marks_of(rec):
        ctx.out.tally["probate records already held to verify a death"] += 1
        return "already held to verify a death"
    ctx.change(u, "add_tags", None, [tag])
    ctx.write("verify deceased", ctx.crm.add_tags, u, [tag], rec)
    ctx.write("verify note", ctx.crm.message, u, VERIFY_NOTE % full_name(person.get("first"), person.get("last")))
    if ctx.live:
        ledger.event(ctx.run_id, ctx.stage.id, u, "skip_verify", {"order": order, "by": by}, now=ctx.now,
                     tz=ctx.policy.tz)
    ctx.out.tally["probate records %s to verify a death" % ("held" if ctx.live else "to hold")] += 1
    ctx.out.line("%s: SmartSkip lists the probate's owner of record as deceased; %s" % (
        addr(rec), "held for research to verify" if ctx.live else "would be held for research to verify"))
    return "held to verify a death" if ctx.live else "would hold to verify a death"


# ---------------------------------------------------------------------- reading the account
def _traced() -> set:
    """Every record any source traced (the ledger's trace rows), in one read."""
    with store._LOCK, store._db() as c:
        return {r["record_uuid"] for r in c.execute("SELECT DISTINCT record_uuid FROM ap_events WHERE action='trace'")}


def _facts(key: str) -> dict:
    out = {}
    with store._LOCK, store._db() as c:
        for r in c.execute("SELECT record_uuid, value FROM ap_facts WHERE key=?", (key,)).fetchall():
            try:
                out[r["record_uuid"]] = json.loads(r["value"])
            except ValueError:
                out[r["record_uuid"]] = r["value"]
    return out


def _landing(ctx: Ctx, uuids: set) -> dict:
    """record uuid -> when it landed in the lane: the lane entry another stage already worked out, else the
    earliest create or list join in the shared log copy. A record neither knows landed before the copy reaches."""
    out = {}
    lane = str(ctx.stage.get("lane") or "ftm")
    for u, v in _facts("lane_entry:%s" % lane).items():
        when = _when(v)
        if u in uuids and when:
            out[u] = when
    missing = uuids - set(out)
    if missing:
        for e in activity.events(("property.created", "property.lists.added"), since=ctx.now - timedelta(days=RECENT_DAYS)):
            r = e.get("resource") or ""
            if r in missing and (r not in out or e["when"] < out[r]):
                out[r] = e["when"]
    return out


def _custom(ctx: Ctx, u: str, counter: dict, fresh: bool = False):
    if not fresh:
        got = ledger.fact(u, "custom_fields", max_age=timedelta(days=7))
        if isinstance(got, dict):
            return got
    try:
        got = ctx.crm.custom_fields(u)
    except Exception as exc:  # noqa: BLE001  (said, and the record waits for the next run)
        ctx.out.problem("could not read a record's custom fields (%s)" % str(exc)[:80])
        return None
    counter["reads"] += 1
    ledger.set_fact(u, "custom_fields", got)
    return got


def _slug(street: str) -> str:
    """The research run folder's name for a house (dream100_dp.slug_of)."""
    return re.sub(r"[^a-z0-9]+", "-", (street or "").lower()).strip("-")


def _lasting(ctx: Ctx, o: dict, rec: dict) -> str:
    """Why a record is passed over for a reason that lasts, or ''. Remembered for recheck_days."""
    ow = rec.get("owner") or {}
    if _is_entity(ow):
        return ENTITY_WHY
    hit = marks_of(rec) & {t.strip().lower() for t in o["skip_tags"]}
    if hit:
        return "research already has it (%s)" % sorted(hit)[0]
    street = (rec.get("address") or {}).get("street") or ""
    if street and (Path(config.RUNS_DIR) / _slug(street) / "smartskip.json").exists():
        return "research already bought SmartSkip for this house"
    custom = ledger.fact(rec.get("uuid") or "", "custom_fields", max_age=timedelta(days=7))
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    signs = gate.deceased_signals(rec, cfg, custom if isinstance(custom, dict) else None)
    if ow.get("deceased") or any(s.startswith("tag deceased") or s.endswith(" field") for s in signs):
        return "the owner's death is already on the record"
    return ""


def _subject(ctx: Ctx, o: dict, rec: dict, counter: dict) -> tuple[str, Subject, dict]:
    """(role, who to trace, what to remember about the row). See the module's docstring."""
    u, ow = rec["uuid"], rec.get("owner") or {}
    base = subject_of(rec)
    meta = {"role": "owner", "owner_uuid": ow.get("uuid") or "", "also": [], "probate": False}
    if not marks_of(rec) & {m.strip().lower() for m in o["probate_marks"]}:
        return "owner", base, meta
    meta["probate"] = True
    custom = _custom(ctx, u, counter) or {}
    names, _ = person_names(_field(custom, o["pr_field"]))
    label = wb.owner_label(ow)
    meta["pr_other"] = bool(names) and not any(wb.same_name(n, label) for n in names)
    df, dl = person_name(_field(custom, o["decedent_field"]))
    if df and dl:
        ga, gb = generation(_field(custom, o["decedent_field"])), generation(ow.get("first_name"), ow.get("last_name"))
        meta["owner_is_decedent"] = (same_person(df, dl, ow.get("first_name"), ow.get("last_name"))
                                     and not (ga and gb and ga != gb))
        prop = place_of(rec)
        subj = Subject(record_uuid=u, owner_uuid=ow.get("uuid") or "", kind="decedent", first=df, last=dl,
                       street=prop["street"], city=prop["city"], state=prop["state"] or "TN", zip=prop["zip"],
                       mail_street=prop["street"], mail_city=prop["city"], mail_state=prop["state"] or "TN",
                       mail_zip=prop["zip"], existing=set(base.existing))
        meta.update(role="decedent", decedent=full_name(df, dl))
        return "decedent", subj, meta
    meta["role"] = "pr"
    return "pr", base, meta


def _fresh(seen: dict, now: datetime, recheck: timedelta) -> bool:
    at = _when((seen or {}).get("at"))
    return bool(at) and now - at < recheck


def _select(ctx: Ctx, o: dict, src, inflight: set) -> tuple[list[Subject], dict, dict]:
    """(who to order, what to remember by primary record, the records read), oldest landing first, inside the
    read cap, the order size and the money caps. Nothing is bought for a record any source already traced."""
    rows = stage_rows(ctx, cap=int(o["max_scan"]))
    ctx.out.found = len(rows)
    traced = _traced()
    skips = _facts(SKIP_FACT)
    when = _landing(ctx, {r["uuid"] for _, r in rows})
    order = {r["uuid"]: i for i, (_, r) in enumerate(rows)}
    far = datetime(1970, 1, 1, tzinfo=timezone.utc)
    rows = sorted(rows, key=lambda tr: ((when.get(tr[1]["uuid"]) or far), order[tr[1]["uuid"]]))
    recheck = timedelta(days=float(o["recheck_days"]))
    unit = float(getattr(src, "unit_usd", 0.15))
    record_cap = o["record_usd"]
    picked: list[Subject] = []
    meta: dict = {}
    cache: dict = {}
    owners: dict = {}
    counter = {"reads": 0}
    capped = ""
    max_n, max_reads = int(o["max_per_order"]), int(o["max_reads"])
    for _, row in rows:
        u = row["uuid"]
        if u in traced:
            ctx.out.tally["already traced (by SmartSkip or another source)"] += 1
            continue
        if u in inflight:
            ctx.out.tally["in a SmartSkip order that is still open"] += 1
            continue
        seen = skips.get(u)
        if isinstance(seen, dict) and _fresh(seen, ctx.now, recheck):
            ctx.out.tally["passed over earlier: %s" % seen.get("why")] += 1
            continue
        if status_of(row) in DEAD_STATUSES:
            continue
        if capped:
            ctx.out.tally["not traced today (%s)" % capped] += 1
            continue
        if len(picked) >= max_n:
            ctx.out.tally["left for the next run (%d an order)" % max_n] += 1
            continue
        if counter["reads"] >= max_reads:
            ctx.out.tally["not read this run (read cap %d)" % max_reads] += 1
            continue
        rec = ctx.crm.record(u)
        counter["reads"] += 1
        if not rec.get("uuid") or status_of(rec) in DEAD_STATUSES:
            continue
        cache[u] = rec
        why = _lasting(ctx, o, rec)
        if why:
            ledger.set_fact(u, SKIP_FACT, {"why": why, "at": ctx.now.isoformat(timespec="seconds")})
            ctx.out.tally["passed over: %s" % why] += 1
            continue
        if not ctx.passes(rec, "trace", row=row, allow={"deceased"} if o["possible_death"] == "trace" else None):
            continue
        role, subj, m = _subject(ctx, o, rec, counter)
        if not (subj.first and subj.last):
            ledger.set_fact(u, SKIP_FACT, {"why": "no first and last name to trace", "at": ctx.now.isoformat(timespec="seconds")})
            ctx.out.tally["passed over: no first and last name to trace"] += 1
            continue
        if role == "owner" and subj.owner_uuid and subj.owner_uuid in owners:
            # One person, one trace: a second house of the same owner rides on the first one's answer.
            meta[owners[subj.owner_uuid]]["also"].append(u)
            ctx.out.tally["shares its owner with another record in the order"] += 1
            continue
        ok, why = ctx.budget(unit * (len(picked) + 1), u, record_usd=unit)
        if ok and record_cap is not None and ledger.spent(record_uuid=u, exclude_action=REFRESH_ACTION) + unit > float(record_cap):
            ok, why = False, "record cap $%.2f lifetime" % float(record_cap)
        if not ok:
            if why.startswith("record cap"):
                ctx.out.tally["at the per-record cap"] += 1
                continue
            capped = why
            ctx.out.tally["not traced today (%s)" % why] += 1
            continue
        picked.append(subj)
        meta[u] = m
        if role == "owner" and subj.owner_uuid:
            owners[subj.owner_uuid] = u
    return picked, meta, cache


# ---------------------------------------------------------------------- landing numbers
def _numbers(phones) -> list[dict]:
    out: list[dict] = []
    for ph in phones or []:
        d = digits((ph or {}).get("number"))
        if d and d not in [n["number"] for n in out]:
            t = str((ph or {}).get("type") or "").upper()
            out.append({"number": d, "type": "MOBILE" if ("WIRELESS" in t or "MOBILE" in t or "CELL" in t)
                        else ("LANDLINE" if "LAND" in t else "UNKNOWN")})
    return out


def land(ctx: Ctx, rec: dict, phones, extra_tag: str = "") -> tuple[int, int]:
    """(callable numbers, numbers landed). The retrace's own landing (scored, tiered, at most keep_numbers, Drop
    marked dead, the line type, the skip trace counter); a number the record already holds from another source
    is marked as two sources agreeing. With `extra_tag` (the representative's role) every number of theirs on the
    owner carries it as well, in one more upsert, recorded with the tags it replaced."""
    subj = subject_of(rec)
    if not subj.owner_uuid:
        return 0, 0
    nums = _numbers(phones)
    on_record = {digits(p.get("number")): p for p in ((rec.get("owner") or {}).get("phones") or [])}
    new = [n for n in nums if n["number"] not in subj.existing]
    # A number already on the record from SmartSkip itself is not a second source agreeing.
    same = [n for n in nums if n["number"] in subj.existing and SOURCE_TAG not in tag_titles(on_record.get(n["number"]) or {})]
    written: dict = {}
    if same:
        _mark_agreed(ctx, rec, subj, same, SOURCE, written)
    good = _write_numbers(ctx, rec, subj, new, SOURCE, written) if new else 0
    landed = [n["number"] for n in new if n["number"] in written]
    if extra_tag:
        before, payload = [], []
        for n in landed + [x["number"] for x in nums if x["number"] in subj.existing]:
            p = written.get(n) or on_record.get(n)
            if not p or extra_tag in tag_titles(p):
                continue
            before.append(phone_payload(p, phone_tier(p)))
            payload.append(phone_payload(p, phone_tier(p), [extra_tag]))
        if payload:
            ctx.change(rec["uuid"], "phones", before, [ph["number"] for ph in payload])
            ctx.write("representative tag", ctx.crm.upsert_phones, subj.owner_uuid, payload)
    return good, len(landed)


# ---------------------------------------------------------------------- the representative
def set_mailing(ctx: Ctx, rec: dict, pr: dict, state: dict) -> str:
    """The owner's mailing address becomes the representative's own when it is blank or the house. The person's
    own address is safe to set on an owner object several records share. The first change of a run is read
    back; one that did not stick stops the address changes for the rest of the run."""
    u, ow = rec["uuid"], rec.get("owner") or {}
    house, cur = place_of(rec), mailing_of(ow)
    if same_place(pr, house):
        ctx.out.tally["representatives who live at the house (mailing kept)"] += 1
        return "the representative lives at the house"
    if cur["street"] and same_place(cur, pr):
        ctx.out.tally["already mails to the representative's own address"] += 1
        return "already mails to the representative's own address"
    if cur["street"] and not same_place(cur, house):
        if ctx.live:
            ledger.set_fact(u, PERSON_FACT, {"at": ctx.now.isoformat(timespec="seconds"), "street": addr(rec),
                                             "why": "mails to a third address",
                                             "current": ", ".join(x for x in (cur["street"], cur["city"], cur["state"], cur["zip"]) if x),
                                             "representative": wb.owner_label(ow),
                                             "representative_address": ", ".join(x for x in (pr["street"], pr["city"], pr["state"], pr["zip"]) if x)})
        ctx.out.tally["mails to a third address: listed for a person"] += 1
        return "mails to a third address: listed for a person"
    if state["stop_address"]:
        return "not changed: an earlier address change this run did not stick"
    owner_uuid = ow.get("uuid") or ""
    if not owner_uuid:
        return "no owner to change"
    raw = ow.get("address") or {}
    before = {"owner_uuid": owner_uuid, "address": {k: raw.get(k) for k in ("street", "city", "state", "postal_code") if k in raw}}
    after = {"street": pr["street"], "city": pr["city"], "state": pr["state"], "postal_code": pr["zip"]}
    ctx.change(u, "owner_address", before, {"owner_uuid": owner_uuid, "address": after})
    ctx.write("mailing address", ctx.crm.update_owner, owner_uuid, {"address": after})
    if ctx.live and not state["address_checked"]:
        state["address_checked"] = True
        try:
            back = mailing_of(ctx.crm.owner(owner_uuid) or {})
        except Exception:  # noqa: BLE001
            back = {}
        if not same_place(back, pr):
            state["stop_address"] = True
            ctx.out.problem("a mailing address change did not stick on %s, so no more addresses were changed this run" % addr(rec))
            return "the mailing address change did not stick"
    ctx.out.tally["mailing %s to the representative's own address" % ("set" if ctx.live else "to set")] += 1
    return "mailing set to the representative's own address" if ctx.live else "would set the mailing to the representative's own address"


def fill_dm(ctx: Ctx, o: dict, rec: dict, counter: dict) -> list[str]:
    """Decision Maker (the representative's full name) and DM Relationship, each only when empty, read fresh
    just before. The county board keeps a probate card in Research until a decision maker is named."""
    u = rec["uuid"]
    custom = _custom(ctx, u, counter, fresh=ctx.live)
    if custom is None:
        return []
    want = {o["dm_field"]: wb.owner_label(rec.get("owner") or {}), o["dm_relationship_field"]: o["pr_relationship"]}
    done = []
    for label, value in want.items():
        if not value or str(_field(custom, label) or "").strip():
            continue
        fid = ctx.crm.custom_field_uuid(label)
        if not fid:
            ctx.out.problem("the custom field %s does not exist in this account, so the representative was not "
                            "written into it" % label)
            continue
        ctx.change(u, "custom_field", {"field_uuid": fid, "value": ""}, {"field_uuid": fid, "value": value})
        ctx.write("decision maker", ctx.crm.set_custom_field, u, fid, value)
        done.append(label)
    if done:
        ctx.out.tally["%s filled with the representative" % " and ".join(done)] += 1
        if ctx.live:
            # The board reads the gate's copy of the fields: give it the names this run wrote.
            ledger.set_fact(u, "custom_fields", dict(custom, **{k: want[k] for k in done}))
    return done


# ---------------------------------------------------------------------- one record's answer
def _apply(ctx: Ctx, o: dict, rec: dict, det: dict | None, m: dict, primary: bool, state: dict,
           order: str, counter: dict) -> tuple[str, dict]:
    """(what happened, what to remember) for one record of a finished order."""
    if det is None:
        ctx.out.tally["SmartSkip returned nobody for the row"] += 1
        return "SmartSkip returned nobody for the row", {}
    role = m.get("role") or "owner"
    ow = rec.get("owner") or {}
    person = {"first": det.get("first"), "last": det.get("last"), "name": det.get("input_name"),
              "property_address": det.get("property_address")}
    rels = det.get("relatives") or []
    if role in ("owner", "pr"):
        if det.get("deceased"):
            verdict, why = death_verdict(ctx, rec, person, o, rels)
            if verdict == "settle":
                return settle_death(ctx, rec, person, order, state), {}
            if verdict == "verify":
                return verify_death(ctx, rec, person, o, order), {}
            ctx.out.tally["death left alone: %s" % why] += 1
            return "death left alone: %s" % why, {}
        if not primary:
            return "shares its owner's numbers with another record", {}
        if not same_person(person["first"], person["last"], ow.get("first_name"), ow.get("last_name")):
            ctx.out.tally["SmartSkip's person is not the owner of record: no numbers landed"] += 1
            return "the person SmartSkip lists is not the owner of record", {}
        ok, why = trace_ok(ctx, rec, o)
        if not ok:
            ctx.out.tally["no numbers landed: %s" % why] += 1
            return "held: %s" % why, {}
        good, n = land(ctx, rec, det.get("subject_phones"))
        if n:
            ctx.out.tally["records given new numbers"] += 1
            ctx.out.line("%s: %d new number(s) from SmartSkip, %d callable" % (addr(rec), n, good))
        return ("%d number(s) landed" % n) if n else "no new number", {}
    # A probate traced through its decedent.
    if m.get("owner_is_decedent"):
        if det.get("deceased"):
            verdict, why = death_verdict(ctx, rec, person, o, rels)
            if verdict == "verify":
                return verify_death(ctx, rec, person, o, order), {"pr_found": False}
            ctx.out.tally["death left alone: %s" % why] += 1
            return "death left alone: %s" % why, {"pr_found": False}
        ctx.out.tally["the owner of record is the person who died: left for research"] += 1
        return "the owner of record is the person who died: left for research", {"pr_found": False}
    if m.get("pr_other"):
        ctx.out.tally["the Personal Representative field names someone other than the owner of record"] += 1
        return "the Personal Representative field names someone else", {"pr_found": False}
    pr, why = find_pr(det, ow.get("first_name"), ow.get("last_name"))
    if pr is None:
        ctx.out.tally[why] += 1
        return why, {"pr_found": False}
    place = relative_place(pr)
    found = {"pr_found": True, "pr": {"name": full_name(pr.get("first"), pr.get("last")), "complete_address": complete(place),
                                      "deceased": bool(pr.get("deceased"))}}
    if pr.get("deceased"):
        verdict, why = death_verdict(ctx, rec, {"first": pr.get("first"), "last": pr.get("last")}, o,
                                     [r for r in rels if r is not pr])
        if verdict == "verify":
            return verify_death(ctx, rec, pr, o, order), found
        ctx.out.tally["death left alone: %s" % why] += 1
        return "death left alone: %s" % why, found
    ctx.out.tally["representatives found among the relatives"] += 1
    parts = []
    ok, why = touch_ok(ctx, rec)
    if ok:
        if fill_dm(ctx, o, rec, counter):
            parts.append("decision maker named")
    else:
        parts.append("fields left alone: %s" % why)
    if not complete(place):
        ctx.out.tally["representatives found without a complete address: no numbers and no address change"] += 1
        parts.append("found without a complete address")
        return "; ".join(parts), found
    ok, why = trace_ok(ctx, rec, o)
    if not ok:
        ctx.out.tally["no numbers landed: %s" % why] += 1
        parts.append("held: %s" % why)
        return "; ".join(parts), found
    good, n = land(ctx, rec, pr.get("phones"), extra_tag=o["pr_tag"])
    if n:
        ctx.out.tally["records given new numbers"] += 1
        parts.append("%d number(s) landed" % n)
    parts.append(set_mailing(ctx, rec, place, state))
    ctx.out.line("%s: representative found; %s" % (addr(rec), "; ".join(parts)))
    return "; ".join(parts), found


def _remember(ctx: Ctx, u: str, det: dict, row: dict, m: dict, order: str, outcome: str, extra: dict) -> None:
    rels = det.get("relatives") or []
    ledger.set_fact(u, FACT, {
        "at": ctx.now.isoformat(timespec="seconds"), "order": order, "stage": ctx.stage.id, "role": m.get("role"),
        "first": row.get("first") or "", "last": row.get("last") or "",
        "found_first": det.get("first") or "", "found_last": det.get("last") or "",
        "deceased": bool(det.get("deceased")), "relatives": len(rels),
        "relatives_deceased": sum(1 for r in rels if isinstance(r, dict) and r.get("deceased")),
        "phones": len(det.get("subject_phones") or []), "hit": bool(det.get("has_results")),
        "pr_found": extra.get("pr_found") if m.get("role") == "decedent" else None, "pr": extra.get("pr"),
        "outcome": outcome, "parsed": det})


def _process(ctx: Ctx, o: dict, src, entry: dict, parsed: list, cache: dict, state: dict, reserved: float) -> int:
    """Use one finished order: the money first (what SmartSkip billed, from what it returned), then per record
    the ledger's trace row and the writes. A record that already has this order's trace row was used by an
    earlier run that stopped part way, and is not written twice. Returns how many records it used."""
    bid = entry["bulkSkipId"]
    rows = entry.get("rows") or []
    src.details, src.costs = {}, {}
    src.match([src.subject_from(r) for r in rows], parsed, order=bid, by_input=True)
    done = {e["record_uuid"] for e in ledger.events(action="trace") if (e.get("detail") or {}).get("order") == bid}
    unit = float(getattr(src, "unit_usd", 0.15))
    billed = sum(unit for r in rows if r.get("record_uuid") not in done
                 and (src.details.get(r.get("record_uuid")) or {}).get("has_results"))
    ctx.settle(reserved, round(billed, 2), "smartskip order %s" % bid)
    counter = {"reads": 0}
    used = 0
    for r in rows:
        primary = r.get("record_uuid") or ""
        m = (entry.get("meta") or {}).get(primary) or {"role": "owner", "also": []}
        det = src.details.get(primary)
        group = [primary] + [x for x in m.get("also") or [] if x]
        for i, u in enumerate(group):
            if u in done:
                continue
            usd = unit if (i == 0 and det and det.get("has_results")) else 0.0
            ledger.event(ctx.run_id, ctx.stage.id, u, "trace",
                         {"source": SOURCE, "order": bid, "role": m.get("role"), "shared_with": primary if i else "",
                          "found": len((det or {}).get("subject_phones") or [])},
                         usd=round(usd, 4), now=ctx.now, tz=ctx.policy.tz)
            used += 1
            rec = cache.get(u)
            if rec is None:
                try:
                    rec = ctx.crm.record(u)
                except Exception as exc:  # noqa: BLE001  (one unreadable record must not stop the order)
                    ctx.out.problem("could not read a traced record (%s)" % str(exc)[:80])
                    continue
            if not (rec or {}).get("uuid"):
                continue
            outcome, extra = _apply(ctx, o, rec, det, m, i == 0, state, bid, counter)
            if det is not None:
                _remember(ctx, u, det, r, m, bid, outcome, extra)
    return used


# ---------------------------------------------------------------------- the person list
def person_file() -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "skipscreen"
    d.mkdir(parents=True, exist_ok=True)
    return d / "for_a_person.csv"


def write_person_file() -> int:
    rows = []
    for u, f in _facts(PERSON_FACT).items():
        if isinstance(f, dict):
            rows.append({"record": u, "street": f.get("street") or "", "why": f.get("why") or "",
                         "current_mailing": f.get("current") or "", "representative": f.get("representative") or "",
                         "representative_address": f.get("representative_address") or ""})
    rows.sort(key=lambda x: (x["street"], x["record"]))
    with person_file().open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["record", "street", "why", "current_mailing", "representative",
                                           "representative_address"])
        w.writeheader()
        for x in rows:
            w.writerow(x)
    return len(rows)


# ---------------------------------------------------------------------- the stage
def _entry_records(entry: dict) -> set:
    out = set()
    for r in entry.get("rows") or []:
        out.add(r.get("record_uuid") or "")
        out.update((((entry.get("meta") or {}).get(r.get("record_uuid") or "") or {}).get("also") or []))
    return out - {""}


def run_skipscreen(ctx: Ctx) -> None:
    st = ctx.stage
    o = settings(st)
    src = (ctx.sources or {}).get(SOURCE)
    if src is None or not hasattr(src, "place"):
        ctx.out.problem("there is no SmartSkip source on this box, so nothing was traced")
        ctx.out.head = "Nothing traced: no SmartSkip source."
        return
    src.card_wait_days = float(o["card_wait_days"])
    ready = src.configured()
    if not ready:
        ctx.out.problem(LOGIN_PROBLEM)
    state = _run_state()
    unit = float(getattr(src, "unit_usd", 0.15))
    used = 0
    blocked = ""
    # 1. Orders an earlier run paid for and could not finish come first, before anything new is ordered.
    open_ = src.pending(st.id)
    inflight = set()
    for e in open_:
        inflight |= _entry_records(e)
    if open_ and not ctx.live:
        ctx.out.line("%d SmartSkip order(s) from an earlier run wait to be collected; the next live run collects them "
                     "before it orders anything" % len(open_))
    elif open_ and ready:
        for e in open_:
            try:
                parsed = src.collect(e, wait_s=o["collect_wait_s"])
            except RuntimeError as exc:
                ctx.out.problem("a SmartSkip order from an earlier run could not be collected (%s)" % str(exc)[:160])
                inflight -= _entry_records(e)
                continue
            if parsed is None:
                blocked = "card" if e.get("status") in ("submitted", "needs_card") else "running"
                continue
            used += _process(ctx, o, src, e, parsed, {}, state, 0.0)
            src.claim(e, "collected by the skip screen")
            inflight -= _entry_records(e)
            ctx.out.tally["orders from an earlier run collected"] += 1
    elif open_:
        blocked = "login"
    if ctx.live and blocked:
        if blocked == "card":
            ctx.out.problem(CARD_PROBLEM)
        elif blocked == "running":
            ctx.out.line("A paid SmartSkip order from an earlier run is still running, so nothing new was ordered; "
                         "the next run collects it first.")
            ctx.out.tally["runs that waited on an earlier order"] += 1
        _head(ctx, used, 0, 0.0)
        return
    if ctx.live and not ready:
        _head(ctx, used, 0, 0.0)
        return
    # 2. Who to trace now.
    picked, meta, cache = _select(ctx, o, src, inflight)
    if not picked:
        _head(ctx, used, 0, 0.0)
        return
    worst = round(unit * len(picked), 2)
    ctx.reserve(worst)
    roles = Counter(m.get("role") for m in meta.values())
    if not ctx.live:
        ctx.settle(worst, 0, SOURCE)
        ctx.out.tally["traces planned via smartskip"] += len(picked)
        ctx.out.line("Would order %d record(s) from SmartSkip, up to $%.2f before scoring (15 cents a record that "
                     "returns anything; scoring is 1.5 cents a number)." % (len(picked), worst))
        _head(ctx, used, len(picked), worst, roles)
        return
    try:
        entry = src.place(picked, st.id, meta)
    except Exception as exc:  # noqa: BLE001  (nothing written; the records stay untraced for the next run)
        ctx.settle(worst, 0, SOURCE)
        ctx.out.problem("SmartSkip failed, nothing was traced: %s" % str(exc)[:200])
        _head(ctx, used, 0, 0.0)
        return
    if entry.get("status") == "needs_card":
        ctx.settle(worst, 0, SOURCE)
        ctx.out.problem(CARD_PROBLEM)
        _head(ctx, used, 0, 0.0)
        return
    try:
        parsed = src.collect(entry, wait_s=o["max_wait_s"])
    except RuntimeError as exc:
        ctx.settle(worst, 0, SOURCE)
        ctx.out.problem("SmartSkip failed, nothing was traced: %s" % str(exc)[:200])
        _head(ctx, used, 0, 0.0)
        return
    if parsed is None:
        ctx.settle(worst, 0, SOURCE)
        ctx.out.line("The SmartSkip order for %d record(s) is paid and still running; the next run collects it "
                     "before ordering more." % len(picked))
        ctx.out.tally["records in an order still running"] += len(picked)
        _head(ctx, used, 0, 0.0)
        return
    used += _process(ctx, o, src, entry, parsed, cache, state, worst)
    src.claim(entry, "used by the skip screen")
    if _facts(PERSON_FACT):
        write_person_file()
        if ctx.out.tally.get("mails to a third address: listed for a person"):
            ctx.out.problem(PERSON_PROBLEM)
    _head(ctx, used, 0, 0.0)


def _head(ctx: Ctx, used: int, planned: int, worst: float, roles: Counter | None = None) -> None:
    t = ctx.out.tally
    if planned:
        roles = roles or Counter()
        ctx.out.acted = planned
        ctx.out.head = ("Would trace %d record(s) through SmartSkip, up to $%.2f before scoring: %d owner(s) of record, "
                        "%d probate decedent(s) to find the representative, %d probate representative(s)." % (
                            planned, worst, roles.get("owner", 0), roles.get("decedent", 0), roles.get("pr", 0)))
        return
    ctx.out.acted = used
    if not used:
        waiting = t.get("runs that waited on an earlier order") or t.get("records in an order still running")
        capped = sum(n for k, n in t.items() if k.startswith("not traced today") or k == "at the per-record cap")
        if waiting:
            ctx.out.head = "Nothing traced: waiting on an open SmartSkip order."
        elif capped:
            ctx.out.head = "Nothing traced: %d record(s) wait on a money cap." % capped
        else:
            ctx.out.head = "No first-to-market record is waiting for a SmartSkip trace."
        return
    dead = sum(n for k, n in t.items() if k.startswith("owners SmartSkip lists as deceased"))
    held = sum(n for k, n in t.items() if k.startswith("probate records") and "verify" in k and "already" not in k)
    ctx.out.head = ("Traced %d record(s) through SmartSkip for $%.2f: %d owner(s) listed as deceased and marked, %d probate "
                    "record(s) held to verify a death, %d representative(s) found, %d record(s) given new numbers." % (
                        used, ctx.out.usd, dead, held, t.get("representatives found among the relatives", 0),
                        t.get("records given new numbers", 0)))


# ---------------------------------------------------------------------- undo (the hook undo.py routes here)
def undo_step(crm, change: dict) -> str:
    """Put one recorded owner change back: the deceased mark the owner had before, or the mailing address. For
    undo.py: `elif what in skipscreen.UNDOABLE: step["do"], step["args"] = "skip_screen", c` and
    `elif s["do"] == "skip_screen": skipscreen.undo_step(crm, s["args"])`."""
    what, before = change.get("what"), change.get("before") or {}
    owner = before.get("owner_uuid") or (change.get("after") or {}).get("owner_uuid")
    if not owner:
        raise RuntimeError("the change does not name the owner")
    if what == "owner_deceased":
        crm.update_owner(owner, {"deceased": bool(before.get("deceased"))})
        return "the owner's deceased mark is back to %s" % bool(before.get("deceased"))
    if what == "owner_address":
        a = before.get("address") or {}
        back = {k: a.get(k) for k in ("street", "city", "state", "postal_code")}
        if not all(back.values()):
            raise RuntimeError("the owner had no complete mailing address before; put it back in DataSift by hand")
        crm.update_owner(owner, {"address": back})
        return "the owner's mailing address is back"
    raise RuntimeError("no undo is written for %r" % what)


# ---------------------------------------------------------------------- the backfill
def may_write(commit: bool) -> bool:
    """The backfill writes only with both switches: --commit on the line and SKIPSCREEN_ALLOW=1 in the
    environment, so a pasted command cannot mark a hundred owners deceased by itself."""
    return bool(commit) and os.environ.get(ALLOW_ENV) == "1"


def _bare_policy():
    return types.SimpleNamespace(gate=gate.GateConfig(), lanes=lanes.Lanes(), tz="America/New_York", stages={},
                                 daily_usd=25.0, record_usd=1.0, outreach_daily_usd=60.0)


def _backfill_stage(pol, stage_id: str = ""):
    stages_ = getattr(pol, "stages", None) or {}
    if stage_id and stage_id in stages_:
        return stages_[stage_id]
    for s in stages_.values():
        if getattr(s, "kind", "") == KIND:
            return s
    from agents.autopilot.policy import Stage
    return Stage(id=stage_id or DEFAULT_STAGE, kind=KIND, owner="research", mode="plan", summary="", schedule=[],
                 presets=[], params={})


def backfill_file(now: datetime) -> Path:
    d = Path(config.DATA_DIR) / "autopilot" / "skipscreen"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("paid_orders_%s.csv" % now.date().isoformat())


def paid_orders(commit: bool = False, *, crm=None, src=None, pol=None, stage_id: str = "", limit: int = 0,
                now: datetime | None = None, find=None) -> dict:
    """Every death SmartSkip already reported on an owner of record, put on the record. Reads every finished
    order (one free download each, kept under <DATA_DIR>/smartskip/orders/ so a rerun downloads nothing),
    matches each subject SmartSkip lists as deceased to its record by the property address and then to the
    owner of record by name, and applies death_verdict exactly as the stage does. Buys nothing. Dry unless
    may_write(commit). Returns the counts by outcome, the CSV of what it did or would do, and the run id."""
    import uuid as _uuid
    if find is None:
        from agents.autopilot.backfill import find_record as find
    if pol is None:
        try:
            from agents.autopilot import policy as policy_mod
            pol = policy_mod.load()
        except Exception:  # noqa: BLE001  (a broken playbook: the gate's defaults still hold)
            pol = None
    st = _backfill_stage(pol, stage_id)
    live = may_write(commit)
    now = now or datetime.now(timezone.utc)
    if crm is None:
        from agents.autopilot.crm import CRM
        crm = CRM()
    src = src or SmartSkip()
    out = Summary(stage=st.id, kind=KIND, owner=st.owner, mode=st.mode, live=live)
    run_id = "skipscreen-paid-" + _uuid.uuid4().hex[:8]
    ctx = Ctx(policy=pol or _bare_policy(), stage=st, crm=crm, run_id=run_id, now=now, live=live, out=out)
    o = settings(st)
    state = _run_state()
    counts: Counter = Counter()
    rows_out: list[dict] = []
    seen: dict = {}
    places: dict = {}
    handled = 0
    try:
        orders = src.completed_orders()
    except RuntimeError as exc:
        orders = []
        out.problem("SmartSkip's order list could not be read (%s), so nothing was decided" % str(exc)[:160])
    for item in orders:
        bid = str(item.get("_id") or item.get("bulkSkipId") or "")
        if not bid:
            continue
        try:
            parsed = src.results(bid)
        except RuntimeError as exc:
            parsed = None
            out.problem("a finished SmartSkip order could not be read (%s)" % str(exc)[:120])
        if parsed is None:
            counts["orders that could not be read"] += 1
            continue
        counts["orders read"] += 1
        for p in parsed:
            if not (isinstance(p, dict) and p.get("deceased")):
                continue
            counts["subjects SmartSkip lists as deceased"] += 1
            street, zip5 = " ".join(str(p.get("property_address") or "").split()), _zip5(p.get("property_zip"))
            row = {"order": bid, "subject": full_name(p.get("first"), p.get("last")), "property": street, "zip": zip5,
                   "record": "", "owner": "", "outcome": "", "why": ""}
            rows_out.append(row)
            if limit and handled >= limit:
                row["outcome"] = "left for a later run (--limit)"
                counts[row["outcome"]] += 1
                continue
            handled += 1
            if not street:
                row["outcome"] = "no property address on the order"
                counts[row["outcome"]] += 1
                continue
            key = (street.upper(), zip5)
            if key not in places:
                try:
                    places[key] = find(crm, street, zip5) or ""
                except Exception as exc:  # noqa: BLE001
                    places[key] = ""
                    out.problem("could not search for a record by address (%s)" % str(exc)[:80])
            u = places[key]
            if not u:
                row["outcome"] = "no record at that address, or more than one"
                counts[row["outcome"]] += 1
                continue
            row["record"] = u
            if u in seen:
                row["outcome"], row["why"] = "the same record again (decided once)", seen[u]
                counts[row["outcome"]] += 1
                continue
            try:
                rec = crm.record(u)
            except Exception as exc:  # noqa: BLE001
                rec = {}
                out.problem("could not read a record (%s)" % str(exc)[:80])
            if not (rec or {}).get("uuid"):
                row["outcome"] = "the record could not be read"
                counts[row["outcome"]] += 1
                continue
            row["owner"] = wb.owner_label(rec.get("owner") or {})
            verdict, why = death_verdict(ctx, rec, p, o, p.get("relatives") or [])
            if verdict == "settle":
                row["outcome"] = settle_death(ctx, rec, p, bid, state, by="paid orders backfill")
            elif verdict == "verify":
                row["outcome"] = verify_death(ctx, rec, p, o, bid, by="paid orders backfill")
            elif verdict == "already":
                row["outcome"] = "already marked deceased"
            else:
                row["outcome"], row["why"] = "left alone", why
            seen[u] = row["outcome"]
            counts[row["outcome"] if row["outcome"] != "left alone" else "left alone: %s" % why] += 1
    path = backfill_file(now)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["order", "subject", "property", "zip", "record", "owner", "outcome", "why"])
        w.writeheader()
        for r in rows_out:
            w.writerow(r)
    return {"live": live, "run": run_id, "counts": dict(counts), "csv": str(path), "problems": list(out.problems),
            "writes": out.writes}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The SmartSkip screen's backfill: deaths SmartSkip already reported, "
                                             "put on the record. Buys nothing.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("paid-orders", help="read every finished SmartSkip order and mark the owners of record it lists "
                                           "as deceased (dry unless --commit and %s=1)" % ALLOW_ENV)
    p.add_argument("--commit", action="store_true", help="write (also needs %s=1)" % ALLOW_ENV)
    p.add_argument("--limit", type=int, default=0, help="decide at most this many deceased subjects")
    p.add_argument("--stage", default="", help="the playbook stage whose settings to use (default: the skip screen's)")
    args = ap.parse_args(argv)
    if args.cmd != "paid-orders":
        ap.print_help()
        return 2
    store.init()
    if args.commit and not may_write(True):
        print("DRY RUN: paid-orders writes only with --commit and %s=1; this run reads and writes nothing." % ALLOW_ENV)
    res = paid_orders(commit=args.commit, limit=args.limit, stage_id=args.stage)
    print("%s: %s" % ("LIVE" if res["live"] else "dry run", ", ".join("%s %d" % (k, v) for k, v in sorted(res["counts"].items()))))
    for k, v in sorted(res["counts"].items(), key=lambda kv: -kv[1]):
        print("  %6d  %s" % (v, k))
    for pr in res["problems"]:
        print("  problem: " + pr)
    print("what it %s: %s" % ("did" if res["live"] else "would do", res["csv"]))
    if res["live"] and res["writes"]:
        print("run %s: python src/agents/cli.py autopilot undo %s --commit puts every change back" % (res["run"], res["run"]))
    return 1 if res["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
