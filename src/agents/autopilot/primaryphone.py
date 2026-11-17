"""The number the team talks to becomes the owner's primary phone (rules L14.95, L5.21).

Why it exists. DataSift's own sequences text the owner's PRIMARY phone (L5.21), and the dialer and the
caller start from it. When a caller marks a number CORRECT, that is the number that reaches the owner,
but the primary stays whatever the data vendor listed first unless somebody changes it by hand. Read
from the account log on 2026-11-15: the lead manager set the primary by hand 8 times in 30 days, 5 of
them within 15 minutes of a phone status change, and nothing did it for her.

What it does. It reads `owner.phone.status.updated` from the brain's copy of the account log (Kixie
writes one each time a caller marks a number; a person in the app writes the same event), and for the
newest number each owner had marked CORRECT it sets that number as the owner's primary phone through
the documented owner update, PATCH /api/internal/owner/{uuid}/. That route's body is the Owner schema,
which carries `primary_phone` as a nullable string in the same "+1..." form as the phone's own number,
so the number is written exactly as the record stores it. The value it replaced is kept (ledger.change)
so `autopilot undo` can put it back; UNDOABLE and undo_step below are the hook for undo.py.

What it never does:
  * set a number whose status now reads anything but CORRECT: a number marked CORRECT and then WRONG,
    DEAD or any do-not-call mark is not the owner's to text;
  * set a number tagged as a litigator's or "dnc", or one the registry scrub flagged (D32: a flagged
    number is never texted, and the primary is what the sequences text);
  * replace a primary that is already a confirmed number: the stage fixes a primary nobody confirmed
    and never picks between two numbers a person confirmed (override_confirmed lifts it);
  * touch a company or trust (people only), or a record the gate holds (Do Not Market, a litigator, an
    opt-out, a sold house, a death).
The published UpdateOwner shape does not list primary_phone, so the route might ignore it without an
error: the first write of every run is read back, and a write that did not stick stops the rest of
the run and says so.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import store
from . import activity, budget, gate, lanes, ledger, scrub
from .stages import Ctx, addr, mask

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

WHAT = "primary_phone"              # the ap_changes kind, and the undo step's name
UNDOABLE = (WHAT,)
ACTION = "primary_phone"            # ap_events: one row per primary set
MARK = "primaryphone"
PLAN_MARK = "primaryphone_plan"
DONE = ("set", "skipped")
MAX_TRIES = 3

DEFAULTS = {
    "sources": [],                  # writers of the event to act on: empty is any (kixie, a person in the app)
    "allow": ["lead", "not_interested"],   # gate words lifted: a confirmed number on a lead or a no is still right
    "override_confirmed": False,    # true replaces a primary that is itself a confirmed number
    "max_owners": 50,               # primaries set (or planned) a run
    "max_reads": 120,               # requests a run for owners' records and records
    "first_days": 7,                # how far back the first run looks
    "sync": True,
    "sync_calls": 60,
}


def settings(st) -> dict:
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    out = []
    for k in ("sources", "allow"):
        v = st.get(k)
        if v is not None and (not isinstance(v, list) or [x for x in v if not str(x or "").strip()]):
            out.append("stage %s: %s must be a list of words" % (st.id, k))
    from .policy import ALLOW_WORDS
    bad = [a for a in (st.get("allow") or []) if str(a) not in ALLOW_WORDS]
    if bad:
        out.append("stage %s: allow has %s (words are %s)" % (st.id, bad, ", ".join(ALLOW_WORDS)))
    for k, lo, hi in (("max_owners", 0, 1000), ("max_reads", 2, 5000), ("first_days", 1, 60), ("sync_calls", 1, 2000)):
        v = st.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            out.append("stage %s: %s must be a whole number from %d to %d" % (st.id, k, lo, hi))
    v = st.get("override_confirmed")
    if v is not None and not isinstance(v, bool):
        out.append("stage %s: override_confirmed is true or false" % st.id)
    return out


# ---------------------------------------------------------------------- the one write, and its read-back
def set_primary(crm, owner_uuid: str, number) -> dict:
    """The only write this stage makes: PATCH /api/internal/owner/{uuid}/ {"primary_phone": number}.
    `number` is the phone exactly as the record stores it (or None, to clear it on an undo). One line
    on purpose: a one-record proof of this helper proves exactly what the stage does."""
    return crm.update_owner(owner_uuid, {"primary_phone": number}) or {}


def read_primary(crm, owner_uuid: str) -> str:
    """The owner's primary phone as the documented owner read returns it ("" for none)."""
    return str((crm.owner(owner_uuid) or {}).get("primary_phone") or "")


def undo_step(crm, change: dict) -> None:
    """Put one recorded change back: the primary the owner had before (None clears it). For undo.py:
        elif what in primaryphone.UNDOABLE: step["do"], step["args"] = "primary_phone", c
        ...
        elif s["do"] == "primary_phone": primaryphone.undo_step(crm, s["args"])"""
    before = change.get("before") if isinstance(change, dict) else None
    if not isinstance(before, dict) or not before.get("owner_uuid"):
        raise RuntimeError("the change does not name the owner")
    set_primary(crm, before["owner_uuid"], before.get("primary_phone") or None)


# ---------------------------------------------------------------------- what a number is
def _find(owner: dict, number: str) -> dict | None:
    for p in owner.get("phones") or []:
        if gate.digits(p.get("number")) == number:
            return p
    return None


def refusal(phone: dict | None, number: str, cfg) -> str:
    """Why this number may never be made the primary, or "". Read off the record as it is NOW, not
    off the event: a number marked CORRECT and then WRONG is not the owner's."""
    if phone is None:
        return "the number is no longer on the owner"
    status = str(phone.get("status") or "UNKNOWN").upper()
    if status != "CORRECT":
        return "the number now reads %s" % status
    tags = {str(t.get("title") if isinstance(t, dict) else t).strip().lower() for t in phone.get("tags") or []}
    if tags & {t.strip().lower() for t in getattr(cfg, "litigator_tags", ["Litigator"])}:
        return "the number is a litigator's"
    if "dnc" in tags:
        return "the number is tagged do not call"
    if getattr(cfg, "registry_flag", True) and scrub.is_flagged(number):
        return "the number is on the do-not-call registry"
    return ""


def _owner_lead_first(rows: list[dict]) -> dict | None:
    """The record to judge an owner by: one in a lead status first, else the first one."""
    for r in rows:
        if lanes.status_of(r) in lanes.LEAD_STATUSES:
            return r
    return rows[0] if rows else None


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


def _mark(subject: str, live: bool) -> dict:
    v = ledger.fact(subject, MARK if live else PLAN_MARK)
    return v if isinstance(v, dict) else {}


def _set_mark(subject: str, live: bool, status: str, now: datetime, **detail) -> None:
    ledger.set_fact(subject, MARK if live else PLAN_MARK, dict(detail, status=status, at=now.isoformat(timespec="seconds")))


def _when(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


# ---------------------------------------------------------------------- the stage
def run_primaryphone(ctx: Ctx) -> None:
    st = settings(ctx.stage)
    out = ctx.out
    live = ctx.live
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    if st["sync"]:
        try:
            activity.sync(ctx.crm, "calls", now=ctx.now, max_calls=int(st["sync_calls"]))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out.problem("could not read the call log (%s), so newly confirmed numbers may be made primary late" % str(exc)[:100])
    kv = "autopilot:primaryphone:%s:%s" % (ctx.stage.id, "live" if live else "plan")
    start = _when(store.kv_get(kv)) or (ctx.now - timedelta(days=float(st["first_days"])))
    sources = {str(s).strip().lower() for s in st["sources"] if str(s).strip()}
    newest: dict[str, dict] = {}
    for e in activity.events(activity.PHONE_STATUS, since=start - activity.OVERLAP, until=ctx.now):
        d = e.get("data") or {}
        if str(d.get("to") or "").strip().upper() != "CORRECT" or not d.get("number"):
            continue
        if sources and str(e.get("source") or "").strip().lower() not in sources:
            continue
        owner = str(e.get("resource") or "")
        if not owner:
            continue
        older = newest.get(owner)
        if older is not None and _mark("primaryphone:%s" % older["id"], live).get("status") not in DONE:
            _set_mark("primaryphone:%s" % older["id"], live, "skipped", ctx.now, why="a later number was confirmed")
        newest[owner] = e
    reads = acted = 0
    open_at: list[datetime] = []
    did, held = [], []
    state = {"verified": False, "broken": False}
    for owner_uuid, e in sorted(newest.items(), key=lambda kv_: kv_[1]["when"]):
        subject = "primaryphone:%s" % e["id"]
        m = _mark(subject, live)
        if m.get("status") in DONE:
            continue
        if state["broken"] or acted >= int(st["max_owners"]) or reads + 2 > int(st["max_reads"]):
            if not state["broken"]:
                out.tally["waiting for a later run (cap)"] += 1
            open_at.append(e["when"])
            continue
        number = gate.digits(e["data"].get("number"))

        def skip(why: str, record: str = "") -> None:
            out.tally["left alone: %s" % why] += 1
            held.append((record, why))
            _set_mark(subject, live, "skipped", ctx.now, why=why)

        try:
            reads += 1
            rows = ctx.crm.owner_records(owner_uuid, cap=10) or []
            row = _owner_lead_first(rows)
            if row is None:
                skip("no record holds this owner")
                continue
            reads += 1
            rec = ctx.crm.record(row["uuid"])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out.problem("could not read an owner whose number was confirmed (%s)" % str(exc)[:80])
            open_at.append(e["when"])
            continue
        if not rec or not rec.get("uuid"):
            skip("the record is gone")
            continue
        street = addr(rec)
        ow = rec.get("owner") or {}
        if str(ow.get("uuid") or owner_uuid) != owner_uuid:
            skip("the record now has another owner", street)
            continue
        if _is_entity(ow):
            skip("a company or trust owns it (people only)", street)
            continue
        phone = _find(ow, number)
        why = refusal(phone, number, cfg)
        if why:
            skip(why, street)
            continue
        if "primary_phone" in ow:
            current_raw = ow.get("primary_phone")
        else:
            reads += 1
            current_raw = read_primary(ctx.crm, owner_uuid) or None
        current = gate.digits(current_raw)
        if current == number:
            skip("it is already the primary", street)
            continue
        if current and not st["override_confirmed"]:
            cur = _find(ow, current)
            if cur is not None and str(cur.get("status") or "").upper() == "CORRECT":
                skip("the primary is already a confirmed number", street)
                continue
        if not ctx.passes(rec, "score", allow=tuple(st["allow"])):
            _set_mark(subject, live, "skipped", ctx.now, why="held back by the gate")
            continue
        before = {"owner_uuid": owner_uuid, "primary_phone": current_raw}
        after = {"owner_uuid": owner_uuid, "primary_phone": phone["number"]}
        ctx.change(rec["uuid"], WHAT, before, after)
        try:
            ctx.write("primary phone", set_primary, ctx.crm, owner_uuid, phone["number"])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            tries = int(m.get("tries") or 0) + 1
            if tries >= MAX_TRIES:
                skip("the write failed %d times" % tries, street)
            else:
                out.problem("could not set the primary phone on %s (%s); it is tried again on the next run" % (street, str(exc)[:80]))
                _set_mark(subject, live, "open", ctx.now, tries=tries)
                open_at.append(e["when"])
            continue
        if live and not state["verified"]:
            state["verified"] = True
            try:
                back = gate.digits(read_primary(ctx.crm, owner_uuid))
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001
                back = ""
            if back != number:
                state["broken"] = True
                out.problem("the primary phone did not stick on %s (wrote the number %s, read back %s); the rest of this "
                            "run sets no primary phone" % (street, mask(number), mask(back) if back else "none"))
                _set_mark(subject, live, "open", ctx.now, tries=int(m.get("tries") or 0) + 1)
                open_at.append(e["when"])
                continue
        acted += 1
        _set_mark(subject, live, "set", ctx.now, record=rec["uuid"])
        if live:
            ledger.event(ctx.run_id, ctx.stage.id, rec["uuid"], ACTION, {"number": number[-4:], "was": current[-4:] if current else ""},
                         now=ctx.now, tz=ctx.policy.tz)
        did.append((street, number, current))
    store.kv_set(kv, (min(open_at) if open_at else ctx.now).isoformat(timespec="seconds"))
    out.found = len(did) + len(held)
    out.acted = len(did)
    if did:
        out.head = "%s the primary phone on %d owner%s to the number a caller just confirmed." % (
            "Set" if live else "Would set", len(did), "" if len(did) == 1 else "s")
        for street, number, was in did[:10]:
            out.line("%s: the number %s%s" % (street, mask(number), (" in place of the number %s" % mask(was)) if was else
                                              " (it had no primary)"))
    elif held:
        top = next((w for w, _ in out.tally.most_common() if w.startswith("left alone: ")), "")
        out.head = "No primary phone to set: %d confirmed number%s left alone (%s)." % (
            len(held), "" if len(held) == 1 else "s", top.replace("left alone: ", "") or "see the lines")


# ---------------------------------------------------------------------- the one-record proof
def prove(crm, owner_uuid: str, number: str, commit: bool = False) -> dict:
    """The proof the orchestrator runs on ONE staging record before this stage goes live: read the
    primary, set it to `number` (as the record stores it), read it back, put the old value back and
    read that back too. Dry unless commit."""
    was = read_primary(crm, owner_uuid)
    out = {"owner": owner_uuid[:8], "was": mask(was) if was else "none", "number": mask(number), "dry": not commit}
    if not commit:
        return out
    set_primary(crm, owner_uuid, number)
    out["read_back"] = mask(read_primary(crm, owner_uuid))
    out["stuck"] = gate.digits(read_primary(crm, owner_uuid)) == gate.digits(number)
    set_primary(crm, owner_uuid, was or None)
    out["restored"] = gate.digits(read_primary(crm, owner_uuid)) == gate.digits(was)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="One-record proof of the primary phone write (dry unless --commit "
                                            "with PRIMARY_PHONE_ALLOW=1).")
    p.add_argument("command", choices=["prove"])
    p.add_argument("--owner", required=True)
    p.add_argument("--number", required=True, help="the phone exactly as the record stores it, e.g. +1 and ten digits")
    p.add_argument("--commit", action="store_true")
    a = p.parse_args(argv)
    if a.commit and os.environ.get("PRIMARY_PHONE_ALLOW") != "1":
        raise SystemExit("--commit writes to the account: set PRIMARY_PHONE_ALLOW=1 as well")
    from .crm import CRM
    print(json.dumps(prove(CRM(), a.owner, a.number, commit=a.commit), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
