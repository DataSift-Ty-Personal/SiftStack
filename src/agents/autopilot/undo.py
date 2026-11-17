"""Put a run back (build plan 0.15).

Every live write records the value it replaced (ledger.change). This module replays a run's
changes backwards, newest first, each through the same documented route that made it:

    assign             back to the previous assignee
    add_tags           the tags are removed
    remove_tags        the tags are added back
    status             the previous status
    call_attempts      the previous counter
    skiptrace_attempts the previous counter
    phones             the tier tags the numbers carried before
    owner              the owner's names and company before an incomplete record was completed
    record_fields      the record fields a stage replaced (taxwatch: the tax delinquent year)
    primary_phone      the owner's primary phone before a confirmed number replaced it
    attempt_note       the attempt log's owner board note: deleted if the run made it, else its text and pin back
    add_numbers        cannot be removed over the API; reported, left in place

Some things cannot be undone and are said so instead of pretended: a mail piece that went out, a
number that was added, a task a teammate already picked up. Dry unless commit, like every command
that writes.
"""
from __future__ import annotations

from . import ledger

IRREVERSIBLE = {"add_numbers": "numbers added to an owner stay (no delete route); their tags can be edited in DataSift",
                "mail": "a mailed piece cannot be recalled"}


def plan(run_id: str) -> list[dict]:
    """The steps an undo would take, newest change first."""
    from .leadroutes import UNDOABLE as LEAD_ROUTES
    from .primaryphone import UNDOABLE as PRIMARY
    out = []
    for c in reversed(ledger.changes(run_id=run_id)):
        what, before, after = c["what"], c["before"], c["after"]
        step = {"change": c["id"], "record_uuid": c["record_uuid"], "what": what, "do": "", "args": None, "note": ""}
        if what in IRREVERSIBLE:
            step["note"] = IRREVERSIBLE[what]
        elif what == "assign":
            if before:
                step["do"], step["args"] = "assign", before
            else:
                step["note"] = "was unassigned before; clear the assignee in DataSift (no documented unassign route)"
        elif what == "add_tags":
            step["do"], step["args"] = "remove_tags", list(after or [])
        elif what == "remove_tags":
            step["do"], step["args"] = "add_tags", list(before or [])
        elif what == "add_lists":
            step["do"], step["args"] = "remove_lists", [t for t in (after or []) if t not in (before or [])]
        elif what == "remove_lists":
            step["do"], step["args"] = "add_lists", [t for t in (before or []) if t not in (after or [])]
        elif what == "status":
            step["do"], step["args"] = "set_status", before
        elif what == "call_attempts":
            step["do"], step["args"] = "call_attempts", int(before or 0)
        elif what == "sms_attempts":
            step["do"], step["args"] = "sms_attempts", int(before or 0)
        elif what in LEAD_ROUTES:
            step["do"], step["args"] = "lead_route", c
        elif what == "task" and after:
            step["do"], step["args"] = "complete_task", str(after)
        elif what == "task_complete":
            step["note"] = "a completed task cannot be reopened through the published API; reopen it in DataSift"
        elif what == "custom_field" and isinstance(before, dict) and (before.get("field_uuid") or (after or {}).get("field_uuid")):
            step["do"], step["args"] = "custom_field", {"field_uuid": before.get("field_uuid") or after.get("field_uuid"),
                                                          "value": before.get("value", "")}
        elif what == "skiptrace_attempts":
            step["do"], step["args"] = "skiptrace_attempts", int(before or 0)
        elif what == "phones":
            step["do"], step["args"] = "upsert_phones", list(before or [])
        elif what == "owner" and isinstance(before, dict) and before.get("owner_uuid"):
            step["do"], step["args"] = "owner_fields", before
        elif what == "record_fields" and isinstance(before, dict) and before:
            step["do"], step["args"] = "record_fields", dict(before)   # taxwatch: the tax year it replaced
        elif what in PRIMARY:
            step["do"], step["args"] = "primary_phone", c             # the owner's primary before a confirmed number
        elif what == "attempt_note":
            step["do"], step["args"] = "attempt_note", c              # the attempt log's note: deleted, or its text back
        else:
            step["note"] = "no undo is written for %r" % what
        out.append(step)
    return out


def run(run_id: str, crm=None, commit: bool = False) -> dict:
    steps = plan(run_id)
    if not steps:
        return {"run": run_id, "steps": 0, "note": "no live change is recorded for that run (or it is already undone)"}
    todo = [s for s in steps if s["do"]]
    cannot = [s for s in steps if not s["do"]]
    if not commit:
        return {"run": run_id, "dry": True, "would_undo": len(todo), "cannot": [{"record": s["record_uuid"][:8], "why": s["note"]} for s in cannot[:10]],
                "steps": [{"record": s["record_uuid"][:8], "do": s["do"], "args": s["args"]} for s in todo[:15]]}
    if crm is None:
        from .crm import CRM
        crm = CRM()
    done, failed = 0, []
    for s in todo:
        u = s["record_uuid"]
        try:
            if s["do"] == "assign":
                crm.assign(u, s["args"])
            elif s["do"] == "remove_tags":
                crm.remove_tags(u, s["args"])
            elif s["do"] == "add_tags":
                crm.add_tags(u, s["args"])
            elif s["do"] == "remove_lists":
                crm.remove_lists(u, s["args"])
            elif s["do"] == "add_lists":
                crm.add_lists(u, s["args"])
            elif s["do"] == "set_status":
                crm.set_status(u, s["args"])
            elif s["do"] == "call_attempts":
                crm.call_attempts(u, s["args"])
            elif s["do"] == "sms_attempts":
                crm.sms_attempts(u, s["args"])
            elif s["do"] == "lead_route":
                from .leadroutes import undo_step
                undo_step(crm, s["args"])
            elif s["do"] == "complete_task":
                from .laneroutes import complete_task
                complete_task(crm, s["args"])
            elif s["do"] == "custom_field":
                crm.set_custom_field(u, s["args"]["field_uuid"], s["args"]["value"])
            elif s["do"] == "skiptrace_attempts":
                owner = (crm.record(u).get("owner") or {}).get("uuid")
                if not owner:
                    raise RuntimeError("the record has no owner now")
                crm.skiptrace_attempts(owner, s["args"])
            elif s["do"] == "upsert_phones":
                owner = (crm.record(u).get("owner") or {}).get("uuid")
                if not owner:
                    raise RuntimeError("the record has no owner now")
                crm.upsert_phones(owner, s["args"])
            elif s["do"] == "owner_fields":
                # The names and the company the owner carried before (complete.py). An override the
                # owner never had cannot be sent back as null, so it is left out and noted.
                b = s["args"]
                fields = {k: b.get(k) for k in ("first_name", "last_name", "company")}
                if b.get("type_override"):
                    fields["type_override"] = b["type_override"]
                crm.update_owner(b["owner_uuid"], fields)
            elif s["do"] == "record_fields":
                from .taxwatch import patch_record
                patch_record(crm, u, s["args"])
            elif s["do"] == "primary_phone":
                from .primaryphone import undo_step
                undo_step(crm, s["args"])
            elif s["do"] == "attempt_note":
                from .attemptlog import undo_step as attempt_undo
                attempt_undo(crm, s["args"])
            ledger.change_undone(s["change"], "undone")
            done += 1
        except Exception as exc:  # noqa: BLE001  (one record failing must not stop the rest going back)
            failed.append({"record": u[:8], "do": s["do"], "error": str(exc)[:160]})
    for s in cannot:
        ledger.change_undone(s["change"], "left: " + s["note"][:80])
    return {"run": run_id, "undone": done, "failed": failed, "left_in_place": len(cannot)}
