"""Workflows the team runs on request: one module each, registered here in one line.

    workflow name (as in team/roster.yaml) -> module in this package

A module exposes two functions:

    run(task: dict) -> dict        does the work. `task` is the queue row: id, agent, workflow,
                                   record_uuid, subject, and its payload (store.payload_of(task)),
                                   which carries `details` (free text from whoever asked) and
                                   `inputs` (the workflow's declared inputs from the roster).
    render(task: dict, result: dict) -> str
                                   the Slack text for the result; "" posts nothing.

Rules every workflow follows (the same ones the autopilot stages follow):
  * It reads through the Open API key only (crm_key_client / autopilot.crm), never a password.
  * It writes nothing to DataSift, sends nothing and pays nobody unless the box is live
    (config.live()) AND the workflow's own spend class allows it. In dry mode it works out what it
    would do and says so.
  * A file it produces goes in the task's run folder under `files/` and is linked with
    slack.file_url(task_id, name), which signs the link.
  * It never raises for an ordinary miss (no record, no comps): it returns a result that says so.

The fixed handlers in worker.HANDLERS and the autopilot's playbook stages are found before this
table, so a name can live in only one place.
"""
from __future__ import annotations

import importlib

REGISTRY: dict[str, str] = {
    "book_appointment": "book_appointment",
    "release_batch": "release_batch",    # queued only by a named person's confirm in Slack (agents/release.py)
    # Atlas, Analysis (wave 4, 2026-11-13)
    "market_report": "market_report",  # Market Finder over the documented API, scored, the county workbook
    "idle_records": "idle_records",    # one list's idle records ranked, with the owners who hold several
    "list_pull": "list_pull",          # ids pulled in by id: quoted first, the first record read back
    "land_desk": "land_desk",          # the Builder Land Desk rendered from a finished analysis on the box
    "account_setup": "account_setup",  # the blueprint planned, checked or applied on the install's own key
    # Diggs, Research (wave 4, 2026-11-13)
    "skip_trace": "skip_trace",                # the autopilot's own retrace waterfall, scoped to one ask
    "phone_score": "phone_score",              # the autopilot's own scorer, scoped to one ask
    "entity_principals": "entity_principals",  # the person behind a company: reverse address, then BusinessV2
    "probate_property": "probate_property",    # the decedent's house on the county roll, owner-of-record guard
    "buyer_registry": "buyer_registry",        # deed-verified buyers, one bounded slice per ask
    # Quinn, Operations (wave 4, 2026-11-13)
    "call_coaching": "call_coaching",      # graded review packs (plan 6.7): queued by coaching_picks or an ask
    "kpi_report": "kpi_report",            # the week's KPIs against team/kpi.yaml, with a workbook
    "qa_batch": "qa_batch",                # a staged batch checked before release; it never releases
    "outreach_health": "outreach_health",  # limits broken, touch audit, caller ID lines to rest, batches waiting
}


def handler(name: str):
    """(run, render) for a registered workflow, or None."""
    module = REGISTRY.get(name)
    if not module:
        return None
    mod = importlib.import_module("%s.%s" % (__package__, module))
    return mod.run, mod.render


def problems() -> list[str]:
    """A registered workflow whose module does not import or lacks run/render. The selftest fails on any."""
    out = []
    for name, module in REGISTRY.items():
        try:
            mod = importlib.import_module("%s.%s" % (__package__, module))
            for fn in ("run", "render"):
                if not callable(getattr(mod, fn, None)):
                    out.append("%s: %s.%s is missing" % (name, module, fn))
        except Exception as exc:  # noqa: BLE001
            out.append("%s: %s could not be loaded (%s)" % (name, module, str(exc)[:120]))
    return out
