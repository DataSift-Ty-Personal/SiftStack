"""Run one autopilot stage and say what happened.

The worker calls `task_handler` for any task whose workflow is a playbook stage (the clock queues
them, an employee's workflow tool queues them, or `cli.py autopilot run` runs one in-process).
`slack_text` renders the result for the owner's channel. A quiet run (nothing found, nothing done,
nothing wrong) renders nothing, so a stage that runs every 30 minutes does not fill a channel.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from .. import config, store
from . import budget
from . import policy as policy_mod
from . import stages

log = logging.getLogger(__name__)

REPORT_KINDS = {"digest", "auction", "capture"}


def _runner(kind: str):
    if kind == "mail":
        from . import mail
        return mail.run_mail
    if kind == "attempts":
        from . import attempts
        return attempts.run_attempts
    if kind == "screen":
        from . import screen
        return screen.run_screen
    if kind == "lanes":
        from . import lanecheck
        return lanecheck.run_lanes
    if kind == "revoke":
        from . import revoke
        return revoke.run_revoke
    if kind == "watchdog":
        from . import watchdog
        return watchdog.run_watchdog
    if kind == "audit":
        from . import audit
        return audit.run_audit
    if kind == "capture":
        from . import capture
        return capture.run_capture
    if kind == "leadcheck":
        from . import leadcheck
        return leadcheck.run_leadcheck
    if kind == "fullattempt":
        from . import fullattempt
        return fullattempt.run_fullattempt
    return {"assign": stages.run_assign, "native_trace": stages.run_native_trace, "retrace": stages.run_retrace,
            "score": stages.run_score, "auction": stages.run_auction, "reactivate": stages.run_reactivate,
            "handoff": stages.run_handoff, "digest": stages.run_digest}[kind]


def is_stage(workflow: str) -> bool:
    try:
        pol = policy_mod.load()
    except policy_mod.PolicyError:
        return workflow in policy_mod.raw_stages()
    return bool(pol and workflow in pol.stages)


def run(stage_id: str, task: dict | None = None, *, plan: bool = False, crm=None, now: datetime | None = None,
        sources=None, scorer=None, mailer=None, pol=None, enqueue=None) -> stages.Summary:
    pol = pol or policy_mod.load()
    if pol is None:
        raise RuntimeError("this install has no team/autopilot.yaml")
    st = pol.stage(stage_id)
    live = st.mode == "live" and config.live() and not plan
    out = stages.Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=live)
    if st.mode == "off":
        out.head = "This stage is off in the playbook."
        return out
    if crm is None:
        from .crm import CRM
        crm = CRM()
    if sources is None:
        from .sources import default_sources
        sources = default_sources()
    if scorer is None:
        from .sources import Scorer
        scorer = Scorer()
    if mailer is None and st.kind == "mail":
        from . import mail
        mailer = mail.vendor_for(st)
    ctx = stages.Ctx(policy=pol, stage=st, crm=crm, run_id=uuid.uuid4().hex[:12],
                     now=now or datetime.now(timezone.utc), live=live, out=out, sources=sources, scorer=scorer,
                     mailer=mailer, task_id=int((task or {}).get("id") or 0), enqueue=enqueue)
    try:
        _runner(st.kind)(ctx)
    except budget.BudgetExhausted as exc:
        # The hour's share of the account's request limit is used. Say so and stop: whatever this
        # run already did is in the ledger, and the stage picks up at its next slot.
        out.problem("stopped early: %s" % exc)
    store.kv_set("autopilot:last:%s" % st.id, json.dumps({
        "at": ctx.now.isoformat(timespec="seconds"), "live": live, "found": out.found, "acted": out.acted,
        "usd": round(out.usd, 2), "would_usd": round(out.would_usd, 2), "problems": out.problems[:5],
        "head": out.head}))
    return out


def task_handler(task: dict) -> dict:
    payload = store.payload_of(task)
    s = run(task["workflow"], task, plan=bool(payload.get("plan")))
    return s.as_dict()


def slack_text(task: dict, result: dict) -> str:
    kind = result.get("kind")
    quiet = not (result.get("acted") or result.get("problems") or result.get("batch"))
    if quiet and kind not in REPORT_KINDS and not (task.get("source") or "").startswith("slack"):
        return ""
    title = (result.get("stage") or task.get("workflow") or "").replace("_", " ")
    mode = "" if result.get("live") else " _(plan: nothing written)_"
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    out = ["%s *%s*%s %s" % (icon, title, mode, result.get("head") or "")]
    for ln in (result.get("lines") or [])[:6]:
        out.append("  %s" % ln)
    for p in (result.get("problems") or [])[:3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)


def plan_all(pol=None, crm=None, now=None, kinds: tuple = ()) -> list[stages.Summary]:
    """Every stage in plan mode, in playbook order: the read-only picture of what the autopilot would
    do right now. Shares one CRM client so the presets are read once."""
    pol = pol or policy_mod.load()
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = []
    for sid, st in pol.stages.items():
        if st.mode == "off" or (kinds and st.kind not in kinds):
            continue
        try:
            out.append(run(sid, plan=True, crm=crm, now=now, pol=pol))
        except Exception as exc:  # noqa: BLE001  (one stage failing must not hide the others)
            s = stages.Summary(stage=sid, kind=st.kind, owner=st.owner, mode=st.mode, live=False)
            s.problem("%s: %s" % (type(exc).__name__, str(exc)[:300]))
            out.append(s)
    return out
