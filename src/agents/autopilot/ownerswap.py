"""The owner of record becomes the decision maker research verified, on estates already researched.

Ty, 2026-11-23: "especially with probates specifically, we want to swap the owner out with the key decision
maker and send our mail to that person, as opposed to just at the house. That way, everything is tied
together."

WHY A STAGE. A courthouse probate already uploads with the court's personal representative as the owner of
record. The records left are the ones whose owner of record is still the person who died: an obituary
record, or a probate that came in from bulk data, once research has named who decides. On those the mail
goes to a dead person at the house, a caller reads a dead person's name, and DataSift's own sequences text
a dead person's primary phone. The research write-back has a guarded owner change (research/writeback.py
swap_plan and swap_owner, behind AGENTS_DP_OWNER_SWAP), but it runs only inside a NEW research run, so an
estate researched before the switch, or while it was off, keeps the dead owner for good. This stage makes
that same change on records research already finished, from the run folder the research left on this box.

WHAT DECIDES, AND WHAT IS NEVER COPIED HERE. The record is assembled from its run folder exactly as the
write-back assembles it (deep_prospect_record.assemble, dream100_dp.decision), and wb.swap_plan decides:
the owner changes only when the owner of record is the person whose death research confirmed, only to a
VERIFIED person (an obituary or court record opened in the research names them living AND a number matched
their name) whom the skip trace does not flag deceased, and only to that person's own complete address,
confirmed by their own trace. wb.swap_owner writes it. Both are imported, never copied, so a rule tightened
there reaches this stage the same day.

REFUSED, EACH COUNTED BY NAME, on top of those rules:
  * the record is a lead: people work a lead, a stage never re-points it;
  * the decision maker reads as a company or a trust (people only);
  * the decision maker's address is incomplete, or their own trace never confirmed one;
  * the skip trace flags the decision maker deceased;
  * two decision makers tie: research names one person and verified another, or verified two and named
    neither, or the record's own Decision Maker field names someone else (a person may have set it);
  * the owner object holds more than one property in the account. The documented update may RENAME the
    owner object instead of attaching a new one (swap_owner says that is not ours to assume), and a rename
    changes the owner of every house the object holds. When every other house is the same estate (nobody
    else on the title, not a lead, not sold) the record is listed for a person, who can decide to move them
    all; otherwise it is refused;
  * a company or a trust holds the title, even beside a person's name (swap_plan refuses only a company with
    no first name; a company on the title is not an estate);
  * the research is older than max_research_days, never wrote back to the record, is running again now, or
    its files are not on this box (or another record on the same street line wrote over its run folder); a
    litigator's number is tied to it; the owner changed since it ran; the owner of record has no complete
    mailing address (the change could never be put back).

WHAT FOLLOWS THE CHANGE, AND WHAT STAYS.
  * The numbers. When DataSift attaches a NEW owner instead of renaming the old one, the numbers stay on the
    old owner object and the record reads as having none (lanes, limits, the county board's dial days and
    the heir lane all key on the owner). So the numbers this research ties to a living person, and every
    number its write-back put on, follow to the new owner exactly as the record holds them (a CORRECT stays
    CORRECT, a WRONG stays WRONG), the way the write-back's own owner change carries them
    (dream100_dp.phase_writeback). A number only the dead owner held, one nobody could place, a stranger's
    and a litigator's stay behind, and a number the new owner already holds is left as it is.
  * The skip trace counter on the new owner goes up by one: research traced this person, and an owner that
    reads "never traced" drops into every Needs Skipped preset.
  * Decision Maker, DM Relationship and Decedent Name are filled when empty (the write-back's own
    "Undetermined" placeholder counts as empty), never overwritten.
  * The death tags and the Obituary list stay: the house still belongs to an estate, and the gate keeps it
    out of cold outreach while the heir lane works it.
  * One note: "Owner of record changed to <name> (<relationship>), the decision maker research verified. The
    decedent stays on the record."
Every write goes through ctx.write and is recorded with what it replaced (ledger.change), so `autopilot undo`
puts the owner back (undo_step below), with the fields and the counter. Numbers carried to a new owner have
no delete route and are reported as such.

TWO SWITCHES. The stage acts only when the playbook marks it live AND the write-back's own switch,
AGENTS_DP_OWNER_SWAP, is on: one switch for the owner change wherever it runs, set once the one-record proof
below has shown which of the two shapes DataSift uses. Until then a live run plans and says so. The kind
spends nothing, so on a box in free mode the playbook's mode is the only other switch.

THE ONE-RECORD PROOF, on a staging account:
    cd src && python -m agents.autopilot.ownerswap proof --record <uuid> [--key-env STAGING_DATASIFT_KEY] [--commit]
changes a test record's owner to an invented person, reads back whether the property moved to a NEW owner or
the old owner object was RENAMED, checks the second owners (a retired route once pushed the old owner into
them), puts it back with the stage's own undo step and reads again. The production key is refused unless
--allow-prod AND OWNERSWAP_PROOF=1.

Known effects of a change, said here so nobody is surprised by them: a record whose owner object is
re-attached starts its owner's call and text history fresh (dial days, text limits and the owner lane are
kept by owner), and the county tax check, which breaks a tie by the owner's name, now holds the heir's name
while the roll keeps the dead owner's. The sold guard reads a deed to the heir as the owner's own purchase,
which is right, where before the change it read as a sale away.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .. import config, store
from ..research import writeback as wb
from . import budget, gate, lanes, ledger
from .crm import titles
from .stages import Ctx, addr, marks_of, tag_titles

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

WHAT = "owner_swap"                  # the ap_changes kind, and the undo step's name
UNDOABLE = (WHAT,)
ACTION = "owner_swap"                # ap_events: one row per owner changed
DONE_FACT = "ownerswap_done"         # live only: this record's owner was changed, so it is never changed again
VERDICT_FACT = "ownerswap_verdict"   # what a read found for one research run (knowledge, kept in plan mode too)
NOTE = "Owner of record changed to %s%s, the decision maker research verified. The decedent stays on the record."
UNDO_NOTE = "Owner change undone: the owner of record is %s again."
UNDO_OTHER = (" DataSift attached an owner with that name and address, so the numbers on the earlier owner "
              "record stay where they were.")
PROOF_NOTE = "Owner swap proof: the owner of record is changed for a moment and put back."
SWITCH_PROBLEM = ("the owner swap waits on its switch: set `AGENTS_DP_OWNER_SWAP` to 1 on the box once the one-record "
                  "proof passes (python -m agents.autopilot.ownerswap proof)")
PERSON_PROBLEM = ("the owner of record also holds other houses of the same estate, so changing it would change them "
                  "too: listed for a person (for example %s)")
# What a Decision Maker or DM Relationship field holds when nobody was determined: the write-back's own
# "Undetermined: <name> has died ..." and "See the research note", and the usual stand-ins.
PLACEHOLDERS = ("undetermined", "not determined", "unknown", "see the research note", "tbd", "n/a", "none")

# swap_plan's refusals, as the stable words a tally counts. Matched on a phrase each refusal carries.
REFUSALS = (
    ("death is not confirmed", "the research does not confirm the owner's death"),
    ("company or a trust", "the owner of record is a company or trust"),
    ("is not the person who died", "the owner of record is not the person who died"),
    ("no verified person", "no verified decision maker"),
    ("as deceased", "the decision maker is flagged deceased"),
    ("own skip trace confirmed", "the decision maker's own trace never confirmed an address"),
    ("no complete mailing address", "the decision maker's address is incomplete"),
    ("no first and last name", "the decision maker has no first and last name"),
)

DEFAULTS = {
    "research_workflow": "deep_prospect",
    "researched_tags": ["Deep Prospected"],   # the research's write-back puts this on; no tag, no write-back
    "death_tags": ["deceased"],               # a death mark: one of these tags, the owner flag, or a death field
    "death_fields": ["Owner Deceased"],
    "max_records": 25,                        # owners changed (or planned) a run
    "max_reads": 150,                         # DataSift reads a run: records, fields, owners, the other houses
    "max_research_days": 120,                 # research older than this is not acted on: a person re-runs it
    "max_shared": 5,                          # other houses of a shared owner read to judge "the same estate"
    "recheck_days": 7,                        # a read that refused a record is trusted this long for that research
    "carry_phones": True,
    "carry_counter": True,
    "fill_fields": True,
    "dm_field": "Decision Maker",
    "relationship_field": "DM Relationship",
    "decedent_field": "Decedent Name",
    "needs_switch": True,                     # act only while AGENTS_DP_OWNER_SWAP is on, as the write-back does
}
_INTS = (("max_records", 0, 500), ("max_reads", 5, 2000), ("max_research_days", 1, 3650), ("max_shared", 0, 50),
         ("recheck_days", 0, 90))
_BOOLS = ("carry_phones", "carry_counter", "fill_fields", "needs_switch")
_LISTS = ("researched_tags", "death_tags", "death_fields")
_TEXTS = ("research_workflow", "dm_field", "relationship_field", "decedent_field")


def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with this stage's block, said when the playbook loads instead of read as a default."""
    out = []
    for k, lo, hi in _INTS:
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            out.append("stage %s: %s must be a whole number from %d to %d" % (st.id, k, lo, hi))
    for k in _BOOLS:
        v = st.get(k)
        if v is not None and not isinstance(v, bool):
            out.append("stage %s: %s is true or false" % (st.id, k))
    for k in _LISTS:
        v = st.get(k)
        if v is not None and (not isinstance(v, list) or not v or any(not isinstance(x, str) or not x.strip() for x in v)):
            out.append("stage %s: %s must be a list of titles" % (st.id, k))
    for k in _TEXTS:
        v = st.get(k)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            out.append("stage %s: %s must be a label" % (st.id, k))
    return out


# ---------------------------------------------------------------------- small readers
def _when(v) -> datetime | None:
    """A stored time as an aware datetime: the task table and the facts table write it a little differently."""
    s = str(v or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s if "T" in s else s.replace(" ", "T", 1))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _days(since, now: datetime) -> float:
    d = _when(since)
    return 1e9 if d is None else (now - d).total_seconds() / 86400.0


def _low(items) -> set[str]:
    return {str(x).strip().lower() for x in items or [] if str(x).strip()}


def address_of(a) -> dict:
    """A mailing address in the four fields DataSift's owner takes, the zip cut to five digits."""
    a = a or {}
    return {"street": " ".join(str(a.get("street") or "").split()), "city": " ".join(str(a.get("city") or "").split()),
            "state": str(a.get("state") or "").strip().upper(),
            "postal_code": str(a.get("postal_code") or a.get("zip") or "").strip()[:5]}


def complete(a: dict) -> bool:
    return bool(a.get("street") and a.get("city") and len(a.get("state") or "") == 2
                and re.fullmatch(r"\d{5}", a.get("postal_code") or ""))


def empty_field(value) -> bool:
    """A field nobody filled, or one that only says nobody was determined."""
    s = str(value or "").strip().lower()
    return not s or any(s.startswith(p) for p in PLACEHOLDERS)


def second_names(rec: dict) -> list[str]:
    """The record's second owners, by name (a row is a dict with full_name, or names, or a plain string)."""
    out = []
    for s in (rec or {}).get("secondary_owners") or []:
        if isinstance(s, dict):
            n = str(s.get("full_name") or " ".join(str(s.get(k) or "") for k in ("first_name", "last_name"))).strip()
        else:
            n = str(s or "").strip()
        if n:
            out.append(n)
    return out


def refusal_key(text: str) -> str:
    low = str(text or "").lower()
    return next((key for phrase, key in REFUSALS if phrase in low), "the research write-back refused the change")


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity       # standard library only; imported late like primaryphone does
    return bool(is_entity(owner or {}))


def owner_is_entity(owner: dict) -> bool:
    """Does a company or a trust hold the title? The shared test (is_entity) passes an owner that carries a
    person's name beside a company, which is right for a call list (the person is a contact there) and wrong
    here: a company on the title is not an estate, and its decision maker is whoever runs it now. A person's
    owner in DataSift carries no company and is typed person."""
    o = owner or {}
    kind = str(o.get("type_override") or o.get("type") or "").strip().lower()
    return _is_entity(o) or bool(str(o.get("company") or "").strip()) or kind in ("llc", "trust")


# ---------------------------------------------------------------------- the research on this box
def research_runs(workflow: str = "deep_prospect") -> dict:
    """record uuid -> the newest FINISHED research on it ({task, finished, result, busy}), in one read of the
    task table. `busy` says a later research task is queued or running, so the run folder may be mid-rewrite."""
    q = ("SELECT t.id, t.record_uuid, t.finished, t.result FROM tasks t JOIN (SELECT record_uuid, MAX(id) AS id "
         "FROM tasks WHERE workflow=? AND status='done' AND record_uuid<>'' GROUP BY record_uuid) m ON t.id = m.id")
    with store._LOCK, store._db() as c:
        rows = c.execute(q, (workflow,)).fetchall()
        busy = {r["record_uuid"] for r in c.execute(
            "SELECT DISTINCT record_uuid FROM tasks WHERE workflow=? AND status IN ('queued','running','waiting') "
            "AND record_uuid<>''", (workflow,)).fetchall()}
    out = {}
    for r in rows:
        try:
            res = json.loads(r["result"] or "{}")
        except ValueError:
            res = {}
        out[r["record_uuid"]] = {"task": int(r["id"]), "finished": str(r["finished"] or ""),
                                 "result": res if isinstance(res, dict) else {}, "busy": r["record_uuid"] in busy}
    return out


def run_folder(res: dict) -> Path | None:
    """Where the research left its files on this box: the folder its result names, else RUNS_DIR/<slug>."""
    slug = str((res or {}).get("slug") or "")
    tries = [Path(str(res["run_dir"]))] if (res or {}).get("run_dir") else []
    if slug and not any(x in slug for x in ("/", "\\", "..")):
        tries.append(Path(config.RUNS_DIR) / slug)
    return next((d for d in tries if (d / "records.json").is_file()), None)


class ReadRun:
    """A research run folder, read only. dream100_dp.Run creates the folder and its subfolders when it
    opens one; this stage reads a folder a research run owns and never writes there."""

    def __init__(self, path):
        self.dir = Path(path)

    def p(self, *parts) -> Path:
        return self.dir.joinpath(*parts)

    def load(self, name, default=None):
        f = self.p(name)
        if not f.is_file():
            return default
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, *a, **k):
        raise RuntimeError("a research run folder is read only here")


def load_case(folder: Path, uuid: str) -> dict:
    """The research on one record, assembled exactly as the write-back assembles it, and the write-back's
    own decision on it (who is verified, which numbers are a litigator's). Nothing is read from the network."""
    import deep_prospect_record as dpr      # standard library only; the research workflow imports it the same way
    dp = dpr.dp
    rn = ReadRun(folder)
    d = next((x for x in dpr.assemble(rn) if str((x.get("x") or {}).get("uuid") or "") == uuid), None)
    if d is None:
        raise LookupError("the run folder is about another record")
    dec = dp.decision(rn, d, wb.settings())
    audit = (rn.load("writeback_audit.json", {}) or {}).get(d.get("slug") or "") or {}
    return {"d": d, "dec": dec, "audit": audit if isinstance(audit, dict) else {}}


def research_numbers(d: dict, audit: dict) -> set[str]:
    """The numbers this research ties to a living person it names (the decision maker, a signer, a family
    line), plus every number its write-back put on the record. A number only the dead owner held, one
    nobody could place, a stranger's (wrong_party) and a litigator's are not among them."""
    dead = wb.dead_names((d or {}).get("an") or {})
    out = set()
    for r in (d or {}).get("dial") or []:
        if r.get("wrong_party") or r.get("litigator"):
            continue
        if any(z.get("name") and not any(wb.same_name(z["name"], x) for x in dead) for z in r.get("reaches") or []):
            out.add(wb.digits(r.get("number")))
    for p in (audit or {}).get("phones_written") or []:
        out.add(wb.digits((p or {}).get("number")))
    out.discard("")
    return out


def named_dm(an: dict) -> list[str]:
    """The people the research's decision maker line names, as the analysis's own names. A line about a role
    ("the executor of the estate") or a name the analysis does not hold names nobody here."""
    people = {}
    for p in (an or {}).get("people") or []:
        nm = wb.plain(p.get("name"))
        if nm:
            people[nm] = [nm] + ([wb.plain(p["rc_name"])] if p.get("rc_name") else [])
    out = []
    raw = re.sub(r"\(.*?\)", " ", str((an or {}).get("decision_maker") or ""))
    for part in re.split(r",|;|&|/|\band\b", raw):
        part = wb.plain(part)
        if len(wb.name_tokens(part)) < 2 or wb.STRUCTURAL.search(part):
            continue
        hit = next((nm for nm, alts in people.items() if any(wb.same_name(a, part) for a in alts)), "")
        if hit and hit not in out:
            out.append(hit)
    return out


def tie(an: dict, verified: dict, person: str) -> str:
    """Why the research does not settle on the one person the change would go to, or ''. The change picks
    the first verified person in the call order; when the research named someone else as the decision
    maker, or verified two people and named neither, that pick is a guess and a person decides."""
    named = named_dm(an)
    good = [n for n, v in (verified or {}).items() if (v or {}).get("verified")]
    if len(named) > 1:
        return "the research names more than one decision maker (%s)" % ", ".join(named)
    if named and not wb.same_name(named[0], person):
        return "the research names %s as the decision maker and verified %s" % (named[0], person)
    if not named and len(good) > 1:
        return "the research verified %s and named no decision maker" % " and ".join(good[:3])
    return ""


def dm_is_entity(plan: dict) -> bool:
    to = (plan or {}).get("to") or {}
    name = " ".join(str(x) for x in ((plan or {}).get("person"), to.get("first_name"), to.get("last_name")) if x)
    return _is_entity({"first_name": to.get("first_name"), "last_name": to.get("last_name")}) or bool(wb.STRUCTURAL.search(name))


def names_person(value: str, person: str) -> bool:
    return wb.named_in(str(value or ""), person) or wb.same_name(wb.plain(value), person)


def death_marked(rec: dict, custom: dict, o: dict) -> bool:
    """A death recorded on the record itself: the owner flag, a death tag, or the Owner Deceased field."""
    if ((rec or {}).get("owner") or {}).get("deceased"):
        return True
    if _low(tag_titles(rec)) & _low(o["death_tags"]):
        return True
    want = _low(o["death_fields"])
    return any(str(k).strip().lower() in want and str(v).strip().lower() in gate.YES for k, v in (custom or {}).items())


def relationship_of(an: dict, plan: dict) -> str:
    """The decision maker's tie to the dead owner, as the research wrote it ("Son"), for DM Relationship."""
    cf = (an or {}).get("crm_fields") or {}
    if names_person(cf.get("Decision Maker") or "", plan["person"]) and str(cf.get("DM Relationship") or "").strip():
        return str(cf["DM Relationship"]).strip()
    for p in (an or {}).get("people") or []:
        if wb.same_name(wb.plain(p.get("name")), plan["person"]):
            r = re.split(r"[(,.;]", str(p.get("relationship") or ""))[0].strip()
            if r:
                return r[:1].upper() + r[1:]
    r = str(plan.get("relationship") or "").strip()
    return r[:1].upper() + r[1:] if r else ""


def decedent_of(an: dict) -> str:
    return (str(((an or {}).get("crm_fields") or {}).get("Decedent Name") or "").strip()
            or wb.plain(((an or {}).get("death") or {}).get("decedent")))


def fields_to_fill(o: dict, custom: dict, plan: dict, an: dict) -> list[tuple[str, str]]:
    """(label, value) for each field this change fills: only a field that is empty or only says nobody was
    determined. A value a person or an earlier research run wrote is never overwritten."""
    out = []
    if empty_field(custom.get(o["dm_field"])):
        out.append((o["dm_field"], plan["person"]))
    rel = relationship_of(an, plan)
    if rel and empty_field(custom.get(o["relationship_field"])):
        out.append((o["relationship_field"], rel))
    dead = decedent_of(an)
    if dead and empty_field(custom.get(o["decedent_field"])):
        out.append((o["decedent_field"], dead))
    return out


def phone_object(p: dict) -> dict:
    """A number exactly as the record holds it, in the shape upsert-phones takes: its status and tags go with
    it, so a number a caller marked WRONG is not dialed again under its new owner."""
    conn = p.get("is_connected")
    return {"number": p.get("number"), "type": str(p.get("type") or "UNKNOWN").upper(), "tags": titles(p.get("tags")),
            "status": str(p.get("status") or "UNKNOWN").upper(), "is_connected": True if conn is None else bool(conn)}


def before_of(u: str, rec: dict) -> dict:
    ow = (rec or {}).get("owner") or {}
    return {"record_uuid": u, "owner_uuid": str(ow.get("uuid") or ""), "first_name": ow.get("first_name"),
            "last_name": ow.get("last_name"), "company": ow.get("company"), "deceased": bool(ow.get("deceased")),
            "address": address_of(ow.get("address"))}


class Seen:
    """The client swap_owner writes through, keeping each answer, so the run learns from the change's own
    read-back whether DataSift renamed the owner or attached a new one, without a read of its own."""

    def __init__(self, crm):
        self.crm = crm
        self.calls: list = []

    def call(self, path, method="GET", body=None, headers=None):
        got = self.crm.call(path, method, body, headers=headers) if headers else self.crm.call(path, method, body)
        self.calls.append((method, path, body, got))
        return got

    def read_back(self, u: str) -> dict:
        path = "/api/internal/property/%s/" % u
        return next((got for m, p, _, got in reversed(self.calls) if m == "GET" and p == path and isinstance(got, dict)), {})

    def posted(self, u: str) -> bool:
        path = "/api/internal/property/%s/message/" % u
        return any(m == "POST" and p == path for m, p, _, _ in self.calls)


def took(rec: dict, to: dict) -> bool:
    ow = (rec or {}).get("owner") or {}
    return (str(ow.get("first_name") or "").strip().lower() == str(to.get("first_name") or "").strip().lower()
            and str(ow.get("last_name") or "").strip().lower() == str(to.get("last_name") or "").strip().lower()
            and bool(str(to.get("first_name") or "").strip()))


def surprises(now: dict, rec: dict, before: dict) -> list[str]:
    """What a change did beyond renaming the owner or attaching a new one: the two things the proof is there
    to rule out. Either one stops the rest of the run."""
    out = []
    if (now or {}).get("address") is not None:
        a, b = address_of(rec.get("address")), address_of(now.get("address"))
        if a["street"].lower() != b["street"].lower():
            out.append("the property address changed")
    was = wb.owner_label({"first_name": before.get("first_name"), "last_name": before.get("last_name")})
    if was and any(wb.same_name(n, was) for n in second_names(now)) and not any(wb.same_name(n, was) for n in second_names(rec)):
        out.append("the old owner was kept as a second owner")
    return out


# ---------------------------------------------------------------------- the stage
def run_ownerswap(ctx: Ctx) -> None:
    o = settings(ctx.stage)
    out = ctx.out
    acting = bool(ctx.live and (wb.settings()["owner_swap"] or not o["needs_switch"]))
    if ctx.live and not acting:
        out.problem(SWITCH_PROBLEM)
    runs = research_runs(str(o["research_workflow"]))
    cases = sorted((info["finished"], u, info) for u, info in runs.items()
                   if str(info["result"].get("owner_status") or "").strip().lower() == "deceased")
    out.found = len(cases)
    st = {"reads": 0, "proven": False, "broken": False, "fields": None}
    did, listed = [], []
    for _, u, info in cases:
        done = ledger.fact(u, DONE_FACT)
        if done:
            out.tally["already changed" if not (isinstance(done, dict) and done.get("pending")) else
                      "already changed, but what follows the change did not all finish (see the change log)"] += 1
            continue
        if st["broken"]:
            out.tally["waiting for a later run (the run stopped)"] += 1
            continue
        if len(did) >= int(o["max_records"]):
            out.tally["waiting for a later run (cap)"] += 1
            continue
        old = ledger.fact(u, VERDICT_FACT)
        if (isinstance(old, dict) and old.get("task") == info["task"]
                and _days(old.get("at"), ctx.now) < float(o["recheck_days"])):
            if old.get("kind") == "person":
                listed.append(old)
                out.tally["left for a person: the owner holds other houses of the same estate"] += 1
            else:
                out.tally["left alone: %s" % old.get("why")] += 1
            continue
        try:
            j = judge(ctx, o, u, info, st)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (one record failing must not stop the others)
            out.problem("could not check a researched estate's owner (%s)" % str(exc)[:100])
            continue
        kind = j["kind"]
        if kind == "wait":
            out.tally[j["why"]] += 1
            continue
        if kind == "error":
            out.problem(j["problem"])
            out.tally["left alone: %s" % j["why"]] += 1
            continue
        if kind == "refused":
            out.tally["left alone: %s" % j["why"]] += 1
            if j.get("remember"):
                ledger.set_fact(u, VERDICT_FACT, {"task": info["task"], "kind": "refused", "why": j["why"],
                                                   "detail": j.get("detail", ""), "at": ctx.now.isoformat(timespec="seconds")})
            continue
        if kind == "person":
            v = {"task": info["task"], "kind": "person", "street": j["street"], "line": j["line"],
                 "at": ctx.now.isoformat(timespec="seconds")}
            ledger.set_fact(u, VERDICT_FACT, v)
            listed.append(v)
            out.tally["left for a person: the owner holds other houses of the same estate"] += 1
            continue
        try:
            r = change_owner(ctx, o, u, info, j, st, acting)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (a change that took is already marked done, pending, so it is never redone)
            out.problem("could not finish the owner change on %s (%s)" % (addr(j.get("rec") or {}), str(exc)[:100]))
            out.tally["left alone: the owner change could not finish"] += 1
            continue
        if r.get("failed"):
            out.tally["left alone: the owner change did not take"] += 1
            continue
        did.append(r)
    _report(ctx, did, listed, acting)


def _no(why: str, detail: str = "", remember: bool = False) -> dict:
    return {"kind": "refused", "why": why, "detail": detail, "remember": remember}


def judge(ctx: Ctx, o: dict, u: str, info: dict, st: dict) -> dict:
    """What happens to one researched estate: swap, refused (and why), for a person, or wait. Everything the
    run folder can answer is answered before a single DataSift read."""
    res = info["result"]
    if info.get("busy"):
        return _no("in research now")
    if _days(info["finished"], ctx.now) > float(o["max_research_days"]):
        return _no("the research is more than %d days old" % int(o["max_research_days"]))
    if res.get("live") is not True or res.get("written_back") is not True:
        return _no("the research never wrote back to the record")
    folder = run_folder(res)
    if folder is None:
        return _no("the research files are not on this box")
    try:
        case = load_case(folder, u)
    except LookupError:
        # Run folders are keyed by the street line, so two records on "12 Oak St" in two towns share one, and the
        # newer research wrote over the older. Not breakage: this record's research is simply no longer here.
        return _no("the run folder now holds another record's research")
    except Exception as exc:  # noqa: BLE001
        return {"kind": "error", "why": "the research files could not be read",
                "problem": "could not read the research files of a researched estate (%s)" % str(exc)[:100]}
    d, dec = case["d"], case["dec"]
    an = d.get("an") or {}
    if dec.get("litigators"):
        return _no("a litigator's number is tied to the research")
    snap = ((d.get("x") or {}).get("record") or {}).get("owner") or {}
    plan, refused = wb.swap_plan(d, dec["verified"], owner=snap)
    if refused or not plan:
        return _no(refusal_key(refused[0] if refused else ""), refused[0] if refused else "")
    if dm_is_entity(plan):
        return _no("the decision maker is a company or trust", plan["person"])
    why = tie(an, dec["verified"], plan["person"])
    if why:
        return _no("two decision makers tie", why)

    # ------------------------------------------------------------------ from here, reads of the record
    if st["reads"] + 3 > int(o["max_reads"]):
        return {"kind": "wait", "why": "waiting for a later run (read cap)"}
    st["reads"] += 1
    rec = ctx.crm.record(u) or {}
    if not rec.get("uuid"):
        return _no("the record is gone", remember=True)
    ow = rec.get("owner") or {}
    if str(ow.get("uuid") or "") != str((d.get("x") or {}).get("owner_uuid") or ""):
        # The research's numbers went onto the owner it read. Another owner now means a person or a later
        # upload moved it, and the numbers and marks may no longer be where the change expects them.
        return _no("the owner changed since the research", remember=True)
    if not marks_of(rec) & _low(o["researched_tags"]):
        return _no("not tagged %s" % o["researched_tags"][0], remember=True)
    if lanes.status_of(rec) in lanes.LEAD_STATUSES:
        return _no("the record is a lead", remember=True)
    st["reads"] += 1
    custom = ctx.crm.custom_fields(u) or {}
    ledger.set_fact(u, "custom_fields", custom)          # the gate's own cache, so it does not read them again
    if not death_marked(rec, custom, o):
        return _no("no death mark on the record", remember=True)
    v = ctx.verdict(rec, "research")
    if not v.ok:
        return _no("held back: %s" % v.reason(), remember=True)
    if owner_is_entity(ow):
        return _no("the owner of record is a company or trust", remember=True)
    plan, refused = wb.swap_plan(d, dec["verified"], owner=ow)
    if refused or not plan:
        return _no(refusal_key(refused[0] if refused else ""), refused[0] if refused else "", remember=True)
    dm_now = str(custom.get(o["dm_field"]) or "")
    if not empty_field(dm_now) and not names_person(dm_now, plan["person"]):
        return _no("two decision makers tie", "the record's %s field names someone else" % o["dm_field"], remember=True)
    if not complete(address_of(ow.get("address"))):
        return _no("the owner of record has no complete mailing address, so a change could not be put back", remember=True)
    # Every house this owner object holds, since a change to the owner reaches all of them. The owner route
    # carries no house count (read live 2026-11-23; properties_count is a list and tag field), so the records
    # search by owner is the count. Unfiltered it answers both of the app's record tabs. It always lists this
    # record under its own owner, so a short list without it is a failed read: wait, never change on a guess.
    # A list cut at the cap is an owner of many houses, judged (and refused) as one below.
    st["reads"] += 1
    cap = int(o["max_shared"]) + 2
    held = ctx.crm.owner_records(ow["uuid"], cap=cap) or []
    if len(held) < cap and not any(str(r.get("uuid") or "") == u for r in held):
        return {"kind": "wait", "why": "waiting for a later run (the owner's houses could not be read)"}
    if len(held) > 1:
        shared = same_estate(ctx, o, ow["uuid"], u, st, rows=held)
        if shared[0] == "wait":
            return {"kind": "wait", "why": "waiting for a later run (read cap)"}
        street = addr(rec)
        if shared[0] == "same":
            others = shared[1]
            line = ("%s: the owner of record also holds %s, so changing it would change %s too; for a person to decide"
                    % (street, ", ".join(others[:3]) + (" and %d more" % (len(others) - 3) if len(others) > 3 else ""),
                       "it" if len(others) == 1 else "them"))
            return {"kind": "person", "street": street, "line": line}
        return _no("the owner holds other houses that are not only this estate", shared[1], remember=True)
    return {"kind": "swap", "rec": rec, "custom": custom, "case": case, "plan": plan}


def same_estate(ctx: Ctx, o: dict, owner_uuid: str, u: str, st: dict, rows: list | None = None) -> tuple:
    """("same", streets) when every other house this owner object holds is part of the same estate: not a
    lead, not sold, and nobody else on its title. ("other", why) when one is not, ("wait",) at the read cap.
    `rows` is the owner's houses when the caller has just read them."""
    if rows is None:
        if st["reads"] + 1 > int(o["max_reads"]):
            return ("wait",)
        st["reads"] += 1
        rows = ctx.crm.owner_records(owner_uuid, cap=int(o["max_shared"]) + 2) or []
    others = [r for r in rows if str(r.get("uuid") or "") != u]
    if not others:
        return ("other", "the owner's other houses could not be found")
    if len(others) > int(o["max_shared"]):
        return ("other", "more houses than the stage reads to judge")
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    streets = []
    for r in others:
        if st["reads"] + 1 > int(o["max_reads"]):
            return ("wait",)
        st["reads"] += 1
        x = ctx.crm.record(r["uuid"]) or {}
        if lanes.status_of(x) in lanes.LEAD_STATUSES:
            return ("other", "another house is a lead")
        if lanes.status_of(x) == "sold" or _low(tag_titles(x)) & _low(cfg.sold_tags):
            return ("other", "another house sold")
        if second_names(x):
            return ("other", "another house has a second owner on the title")
        streets.append(addr(x))
    return ("same", streets)


def field_ids(ctx: Ctx, o: dict, st: dict) -> dict:
    """label -> field uuid for the three fields this stage fills, read once a run. A field the account does not
    have is said once (a setup a person makes) and skipped."""
    if st["fields"] is None:
        st["fields"] = {}
        st["reads"] += 1
        for label in (o["dm_field"], o["relationship_field"], o["decedent_field"]):
            fid = ctx.crm.custom_field_uuid(label) or ""
            if fid:
                st["fields"][label] = fid
            else:
                ctx.out.problem("the %s custom field does not exist in this account, so it is not filled" % label)
    return st["fields"]


def change_owner(ctx: Ctx, o: dict, u: str, info: dict, j: dict, st: dict, acting: bool) -> dict:
    """The change and everything that follows it, in this order: the owner (with its one note), the research's
    numbers and the skip trace counter when a new owner was attached, then the empty fields. Each live write is
    recorded with what it replaced before it is made. Returns what was done, or what would be."""
    rec, custom, case = j["rec"], j["custom"], j["case"]
    d = case["d"]
    an = d.get("an") or {}
    plan = dict(j["plan"])
    rel = relationship_of(an, plan)
    plan["note"] = NOTE % (plan["person"], " (%s)" % rel.lower() if rel else "")
    old = rec.get("owner") or {}
    street = addr(rec)
    nums = research_numbers(d, case["audit"])
    follow = [p for p in old.get("phones") or [] if wb.digits(p.get("number")) in nums]
    fills = fields_to_fill(o, custom, plan, an) if o["fill_fields"] else []
    ids = field_ids(ctx, o, st) if fills else {}
    fills = [(k, v) for k, v in fills if k in ids]
    to = plan["to"]
    report = {"uuid": u, "street": street, "was": wb.owner_label(old), "person": plan["person"], "rel": rel,
              "city": to["address"].get("city"), "state": to["address"].get("state"),
              "follow": len(follow) if o["carry_phones"] else 0, "fields": [k for k, _ in fills], "done": False}
    if not acting:
        ctx.out.writes += 1 + len(fills)
        return report

    before = before_of(u, rec)
    ctx.change(u, WHAT, before, {"person": plan["person"], "first_name": to["first_name"], "last_name": to["last_name"],
                                 "address": dict(to["address"])})
    seen = Seen(ctx.crm)
    err = ""
    try:
        done = ctx.write("owner of record", wb.swap_owner, seen, u, plan) or {}
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001  (a step after the change may be what failed: the read-back below decides)
        err, done = str(exc)[:120], {}
    now = seen.read_back(u)
    if err and not now:
        try:
            now = ctx.crm.record(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001
            now = {}
    if not (done.get("ok") or took(now, to)):
        ctx.out.problem(("could not change the owner on %s (%s); it is tried again on the next run" % (street, err)) if err
                        else "the owner change did not stick on %s; it is tried again on the next run" % street)
        return dict(report, failed=True)
    new = (now or {}).get("owner") or {}
    new_uuid = str(done.get("owner_uuid") or new.get("uuid") or "")
    same = new_uuid == str(old.get("uuid") or "")
    # Marked at once, so a run the request budget stops part way can never change this owner a second time;
    # `pending` says the writes that follow the change have not all been made yet.
    mark = {"at": ctx.now.isoformat(timespec="seconds"), "task": info["task"], "person": plan["person"],
            "before": before["owner_uuid"], "after": new_uuid, "same_owner": same, "pending": True}
    ledger.set_fact(u, DONE_FACT, mark)
    if err and not seen.posted(u):
        try:
            ctx.write("note", ctx.crm.message, u, plan["note"])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not post the owner change note on %s (%s)" % (street, str(exc)[:80]))
    if not st["proven"]:
        st["proven"] = True
        odd = surprises(now, rec, before)
        if odd:
            st["broken"] = True
            ctx.out.problem("the first owner change of the run did not behave as the proof showed on %s (%s): no other "
                            "owner is changed this run, and undo puts this one back" % (street, "; ".join(odd)))
    carried, counter = 0, None
    if not same and new_uuid:
        have = {wb.digits(p.get("number")) for p in new.get("phones") or []}
        items = [phone_object(p) for p in follow if wb.digits(p.get("number")) not in have]
        if items and o["carry_phones"]:
            ctx.change(u, "add_numbers", [], [wb.digits(p["number"])[-4:] for p in items])
            try:
                ctx.write("numbers to the new owner", ctx.crm.upsert_phones, new_uuid, items)
                carried = len(items)
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not carry the research's numbers to the new owner on %s (%s)" % (street, str(exc)[:80]))
        if o["carry_counter"]:
            cur = done.get("skiptrace_attempts")
            cur = int(cur if cur is not None else new.get("skiptrace_attempts") or 0)
            ctx.change(u, "skiptrace_attempts", cur, cur + 1)
            try:
                ctx.write("skip trace counter", ctx.crm.skiptrace_attempts, new_uuid, cur + 1)
                counter = cur + 1
            except budget.BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                ctx.out.problem("could not write the skip trace counter on %s (%s)" % (street, str(exc)[:80]))
    filled = []
    for label, value in fills:
        ctx.change(u, "custom_field", {"field_uuid": ids[label], "value": str(custom.get(label) or "")},
                   {"field_uuid": ids[label], "value": value})
        try:
            ctx.write("field %s" % label, ctx.crm.set_custom_field, u, ids[label], value)
            filled.append(label)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.out.problem("could not fill the %s field on %s (%s)" % (label, street, str(exc)[:80]))
    ledger.event(ctx.run_id, ctx.stage.id, u, ACTION, {"person": plan["person"], "same_owner": same, "carried": carried,
                                                       "counter": counter, "fields": filled, "task": info["task"]},
                 now=ctx.now, tz=ctx.policy.tz)
    ledger.set_fact(u, DONE_FACT, dict(mark, pending=False, carried=carried, counter=counter, fields=filled))
    return dict(report, done=True, same=same, carried=carried, counter=counter, fields=filled)


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _line(r: dict, acting: bool) -> str:
    who = "%s%s" % (r["person"], " (%s)" % r["rel"].lower() if r.get("rel") else "")
    where = ", ".join(x for x in (r.get("city"), r.get("state")) if x)
    if not r.get("done"):
        tail = ""
        if r.get("fields"):
            tail += "; fills %s" % ", ".join(r["fields"])
        if r.get("follow"):
            tail += "; %s follow if DataSift attaches a new owner" % _plural(r["follow"], "research number")
        return "%s: would change the owner of record from %s to %s, with mail to their own address in %s%s" % (
            r["street"], r["was"] or "the person who died", who, where, tail)
    tail = ""
    if r.get("carried"):
        tail += "; %s carried to the new owner" % _plural(r["carried"], "number")
    if r.get("counter") is not None:
        tail += "; skip trace counter %d" % r["counter"]
    if r.get("fields"):
        tail += "; filled %s" % ", ".join(r["fields"])
    return "%s: owner of record changed from %s to %s, mail to their own address in %s%s" % (
        r["street"], r["was"] or "the person who died", who, where, tail)


def _report(ctx: Ctx, did: list, listed: list, acting: bool) -> None:
    out = ctx.out
    # A live run whose switch is off changed nothing, so it counts nothing as done: the sweep posts a live run
    # that acted as an update, and "would change" every morning is not one. A plan run counts what it would do.
    out.acted = len(did) if (acting or not ctx.live) else 0
    for r in did[:10]:
        out.line(_line(r, acting))
    for v in listed[:5]:
        if v.get("line"):
            out.line(v["line"])
    if listed:
        out.problem(PERSON_PROBLEM % (listed[0].get("street") or "a researched estate"))
    if did and acting:
        out.head = ("Changed the owner of record to the decision maker research verified on %s; the decedent stays "
                    "on each record." % _plural(len(did), "estate record"))
    elif did:
        out.head = "Would change the owner of record to the decision maker research verified on %s%s" % (
            _plural(len(did), "estate record"), "; the change waits on its switch." if ctx.live else ".")
    elif listed:
        out.head = ("No owner changed: %s %s for a person, because the owner also holds other houses of the estate."
                    % (_plural(len(listed), "estate record"), "waits" if len(listed) == 1 else "wait"))
    elif not out.found:
        out.head = "No researched estate on this box: no finished research has found the owner of record dead."
    else:
        top = next((k for k, _ in out.tally.most_common() if k.startswith("left alone: ")), "")
        out.head = "No owner to change: %s left alone (%s)." % (
            _plural(out.found, "researched estate"), top.replace("left alone: ", "") or "see the lines")


# ---------------------------------------------------------------------- undo
def put_back(crm, u: str, before: dict) -> dict:
    """The owner the record held before, through the same documented update the change made: the name and the
    mailing address on the property's owner. Whichever shape DataSift used for the change, it uses again: a
    renamed owner object is renamed back, and a property moved to a new owner finds the earlier owner by its
    name and address. A deceased mark the change cleared is put back on whichever owner the record holds."""
    a = address_of(before.get("address"))
    first, last = str(before.get("first_name") or "").strip(), str(before.get("last_name") or "").strip()
    if not (first and last and complete(a)):
        raise RuntimeError("the change does not hold the owner's full name and mailing address before it, so the owner "
                           "was not put back")
    body = {"first_name": first, "last_name": last, "address": a}
    if before.get("company"):
        body["company"] = before["company"]
    crm.call("/api/internal/property/%s/" % u, "PATCH", {"owner": body})
    now = (crm.record(u) or {}).get("owner") or {}
    if not took({"owner": now}, body):
        raise RuntimeError("the owner did not go back to %s %s" % (first, last))
    if before.get("deceased") and not now.get("deceased") and now.get("uuid"):
        crm.update_owner(now["uuid"], {"deceased": True})
    return {"owner_uuid": str(now.get("uuid") or ""), "same_object": str(now.get("uuid") or "") == str(before.get("owner_uuid") or "")}


def undo_step(crm, change: dict) -> None:
    """Put one recorded owner change back, with a note that says so. For undo.py:
        elif what in ownerswap.UNDOABLE: step["do"], step["args"] = "owner_swap", c
        ...
        elif s["do"] == "owner_swap": ownerswap.undo_step(crm, s["args"])
    undo replays a run newest first, so the fields and the counter that followed the change go back before it."""
    before = change.get("before") if isinstance(change, dict) else None
    if not isinstance(before, dict) or not before.get("owner_uuid"):
        raise RuntimeError("the change does not name the owner before it")
    u = str(change.get("record_uuid") or before.get("record_uuid") or "")
    if not u:
        raise RuntimeError("the change does not name the record")
    got = put_back(crm, u, before)
    note = UNDO_NOTE % wb.owner_label({"first_name": before.get("first_name"), "last_name": before.get("last_name")})
    crm.message(u, note + ("" if got["same_object"] else UNDO_OTHER))


# ---------------------------------------------------------------------- the one-record proof
def prove(crm, uuid: str, commit: bool = False, first: str = "Proof", last: str = "Swaptest") -> dict:
    """The proof the orchestrator runs on ONE staging record before this stage goes live. It changes the
    record's owner through the stage's own write (wb.swap_owner) to an invented person at the property's
    address, reads back which shape DataSift used and what the second owners show, puts it back through the
    stage's own undo step, and reads again. Dry unless commit. It refuses a record whose owner object holds
    any other property, since a rename there would move every one of them, and a lead."""
    rec = crm.record(uuid) or {}
    ow = rec.get("owner") or {}
    out = {"proof": "owner_swap", "blocks": "estate_owner_swap", "record": uuid, "dry": not commit,
           "sent": "%s %s" % (first, last)}
    if not ow.get("uuid"):
        return dict(out, ok=False, note="the record has no owner")
    if lanes.status_of(rec) in lanes.LEAD_STATUSES:
        return dict(out, ok=False, note="the record is a lead: prove on a record made for the purpose")
    held = crm.owner_records(ow["uuid"], cap=5) or []       # the owner route carries no house count
    if len(held) > 1:
        return dict(out, ok=False, note="the owner holds %s records; prove on a record whose owner holds only this one"
                    % ("%d or more" % len(held) if len(held) >= 5 else len(held)))
    if not any(str(r.get("uuid") or "") == uuid for r in held):
        return dict(out, ok=False, note="could not read which records the owner holds")
    before = before_of(uuid, rec)
    if not (complete(before["address"]) and str(before["first_name"] or "").strip() and str(before["last_name"] or "").strip()):
        return dict(out, ok=False, note="the owner has no full name or no complete mailing address, so the change could "
                                        "not be put back")
    to_addr = address_of(rec.get("address"))
    if not complete(to_addr):
        to_addr = dict(before["address"])
    plan = {"person": "%s %s" % (first, last), "relationship": "", "before": before, "note": PROOF_NOTE,
            "to": {"first_name": first, "last_name": last, "address": to_addr}}
    if not commit:
        return dict(out, ok=None, note="dry: would change the owner of record to %s %s, read back whether DataSift renamed the "
                                       "owner or attached a new one and what the second owners show, put it back with the "
                                       "stage's own undo step and read again" % (first, last))
    base_second = second_names(rec)
    done = wb.swap_owner(crm, uuid, plan)
    after = crm.record(uuid) or {}
    aow = after.get("owner") or {}
    was = wb.owner_label(ow)
    out["took"] = bool(done.get("ok"))
    out["behavior"] = "renamed the owner object" if str(aow.get("uuid") or "") == before["owner_uuid"] else "attached a new owner object"
    out["old_owner_now"] = wb.owner_label(crm.owner(before["owner_uuid"]) or {})
    out["old_in_second_owners"] = (any(wb.same_name(x, was) for x in second_names(after))
                                   and not any(wb.same_name(x, was) for x in base_second))
    out["property_address_kept"] = (address_of(after.get("address"))["street"].lower()
                                     == address_of(rec.get("address"))["street"].lower())
    out["phones_on_record"] = "%d before, %d after" % (len(ow.get("phones") or []), len(aow.get("phones") or []))
    out["deceased_after"] = bool(aow.get("deceased"))
    try:
        undo_step(crm, {"record_uuid": uuid, "before": before})
        out["undo"] = "ran"
    except Exception as exc:  # noqa: BLE001  (a refused route is the finding, not a crash)
        out["undo"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    final = crm.record(uuid) or {}
    fow = final.get("owner") or {}
    out["restored"] = (str(fow.get("uuid") or "") == before["owner_uuid"] and took({"owner": fow}, before)
                       and address_of(fow.get("address")) == before["address"]
                       and bool(fow.get("deceased")) == before["deceased"])
    out["second_owners_restored"] = sorted(second_names(final)) == sorted(base_second)
    out["ok"] = bool(out["took"] and out["restored"] and out["second_owners_restored"]
                     and out["property_address_kept"] and not out["old_in_second_owners"])
    out["note"] = "DataSift %s; %s" % (out["behavior"], "put back as it was" if out["restored"] else "NOT put back as it was")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="One-record proof of the owner swap: change a test record's owner, read back "
                                            "which shape DataSift used, put it back (dry unless --commit).")
    p.add_argument("command", choices=["proof"])
    p.add_argument("--record", required=True, help="the uuid of a record made for the proof")
    p.add_argument("--key-env", default="STAGING_DATASIFT_KEY",
                   help="the environment variable that holds the TARGET account's Open API key")
    p.add_argument("--allow-prod", action="store_true", help="permit the production key (also needs OWNERSWAP_PROOF=1)")
    p.add_argument("--first", default="Proof")
    p.add_argument("--last", default="Swaptest")
    p.add_argument("--commit", action="store_true")
    a = p.parse_args(argv)
    key = os.environ.get(a.key_env or "", "")
    if not key:
        print("name the environment variable that holds the staging account's Open API key: --key-env STAGING_DATASIFT_KEY")
        return 2
    prod = os.environ.get("REISIFT_API_KEY") or ""
    if prod and key == prod and not (a.allow_prod and os.environ.get("OWNERSWAP_PROOF") == "1"):
        print("that is the production key. The proof runs on staging; on production it needs --allow-prod and "
              "OWNERSWAP_PROOF=1, and only on a record made for it.")
        return 3
    from datasift_api_upload import Api
    from .crm import CRM
    from .proofs import _log
    r = prove(CRM(api=Api(api_key=key)), a.record, commit=a.commit, first=a.first, last=a.last)
    r["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _log(r)
    print(json.dumps(r, indent=1, default=str))
    if not a.commit:
        print("DRY RUN: nothing was written. Re-run with --commit.")
    return 1 if r.get("ok") is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
