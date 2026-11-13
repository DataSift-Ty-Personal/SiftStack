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
            ledger.change_undone(s["change"], "undone")
            done += 1
        except Exception as exc:  # noqa: BLE001  (one record failing must not stop the rest going back)
            failed.append({"record": u[:8], "do": s["do"], "error": str(exc)[:160]})
    for s in cannot:
        ledger.change_undone(s["change"], "left: " + s["note"][:80])
    return {"run": run_id, "undone": done, "failed": failed, "left_in_place": len(cannot)}
