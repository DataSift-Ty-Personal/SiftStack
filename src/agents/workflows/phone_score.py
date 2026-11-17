"""phone_score: every unscored number on a record or a small preset tiered, written back as the
autopilot's scoring stage writes it.

It IS the scoring stage, built for one ask with the settings of the playbook's own (AGENTS_PHONE_
SCORE_LIKE, default ftm_score) and handed only the records named: Trestle with the litigator
check (agents.autopilot.sources.Scorer), every score cached by number so a number on a second
record costs nothing, exactly one tier per phone (a union would leave a re-scored phone in two
dialer presets at once), a Drop number marked dead and the line type written where DataSift has
none (when the playbook's stage says so), a litigator's number suppressing every record the owner
holds, and the gate before any record is read for scoring. A record the stage scored inside its
recheck window (7 days) is not scored again.

Money: Trestle runs only on a live box, inside AGENTS_PHONE_SCORE_RUN_USD for this run, the
autopilot's data cap, the brain's daily cap and the per-record lifetime cap. In dry mode it prices
the numbers and writes nothing. After a live run every record it changed is read back.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .. import config
from ..autopilot import ledger, stages
from . import skip_trace as common

WORKFLOW = "phone_score"
SCORE_KEEP = ("drop_status", "write_line_type", "recheck_days", "max_reads")


def run(task: dict, *, crm=None, pol=None, scorer=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if pol is None:
        pol, note = common.load_policy()
    else:
        note = ""
    crm = crm or common.default_crm()
    target = common.resolve_target(task, crm, now)
    if not target["kind"]:
        return {"ok": False, "say": target["say"]}
    rows, first, scope = common.scope_rows(task, crm, now, target)
    params = common.base_params(pol, os.environ.get("AGENTS_PHONE_SCORE_LIKE") or "ftm_score", SCORE_KEEP)
    params.update({"max_records": max(1, len(rows)), "max_reads": max(1, len(rows)), "max_scan": max(1, len(rows))})
    recheck = timedelta(days=float(params.get("recheck_days", 7)))
    recent = [r for r in rows if ledger.fact(r["uuid"], "scored", max_age=recheck)]
    result = {"ok": True, "live": config.live(), **scope, "held": [],
              "run_cap_usd": common.setting("AGENTS_PHONE_SCORE_RUN_USD", 1.0)}
    problems = ([note] if note else []) + ["%s: %s" % (scope.get("title"), n) for n in scope.get("notes") or []]
    if problems:
        result["problems"] = problems
    for r in recent:
        result["held"].append({"record": r["uuid"], "street": stages.addr(first.get(r["uuid"]) or r),
                               "why": "scored in the last %d days; nothing new to score" % recheck.days})
    rows = [r for r in rows if r not in recent]
    if not rows:
        result.update(head="Nothing to score.", acted=0, usd=0.0, would_usd=0.0)
        return result
    from ..autopilot.sources import Scorer
    ctx = common.stage_ctx(task, pol, common.Scoped(crm, rows, first), kind="score", stage_id=WORKFLOW,
                           params=params, run_cap=result["run_cap_usd"], now=now,
                           scorer=scorer if scorer is not None else Scorer())
    common.run_stage(ctx, stages.run_score)
    out = ctx.out
    checked, rb = common.read_back(crm, ctx.run_id) if ctx.live else (0, [])
    head = out.head
    if not out.acted and not out.problems and not any(k.startswith("held back") for k in out.tally):
        head = "Nothing to score: every number on it already carries a tier."
    held = [{"record": "", "street": "%d record(s)" % n, "why": k} for k, n in out.tally.items() if k.startswith("held back")]
    result.update(head=head, lines=out.lines, problems=list(result.get("problems") or []) + out.problems + rb,
                  tally=dict(out.tally), acted=out.acted, usd=round(out.usd, 2), would_usd=round(out.would_usd, 2),
                  writes=out.writes, run_id=ctx.run_id, read_back=checked, held=result["held"] + held)
    return result


def render(task: dict, result: dict) -> str:
    return common.render_people("phone score", result)
