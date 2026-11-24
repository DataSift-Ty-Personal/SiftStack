"""Run one autopilot stage and say what happened.

The worker calls `task_handler` for any task whose workflow is a playbook stage (the clock queues
them, an employee's workflow tool queues them, or `cli.py autopilot run` runs one in-process).
`slack_text` renders the result for the owner's channel. A quiet run (nothing found, nothing done,
nothing wrong) renders nothing, so a stage that runs every 30 minutes does not fill a channel. A
stage in plan mode says its plan once a day, for the same reason: it writes nothing, so every run
finds the same work again.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .. import config, store
from . import budget
from . import policy as policy_mod
from . import registry
from . import stages

log = logging.getLogger(__name__)

REPORT_KINDS = {"digest", "auction", "capture"} | {k for k, row in registry.EXTRA.items() if row[4]}


def _runner(kind: str):
    if kind in registry.EXTRA:
        return registry.runner(kind)
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
    live = st.mode == "live" and config.stage_live(st.spend, st.id) and not plan
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
        "head": out.head,
        # The run's own lines, so team_query can give the full list a six-line Slack report leaves out.
        # Never on the public health page (brain.PUBLIC_LAST): a line names streets.
        "lines": [str(x) for x in out.lines if x != "..."]}))
    return out


def task_handler(task: dict) -> dict:
    payload = store.payload_of(task)
    s = run(task["workflow"], task, plan=bool(payload.get("plan")))
    return s.as_dict()


def plan_repeat(task: dict, result: dict, now: datetime | None = None) -> bool:
    """True when a scheduled plan run has nothing new to say. A stage in plan mode writes nothing,
    so it finds the same work on every run: said once a day that is a picture of what the stage
    would do, said every 30 minutes it buries the channel. So a plan run posts the first time each
    business day, and again that day only when it has MORE problems than it had then. A live run,
    an ask from Slack and a run from the command line are never held back."""
    if result.get("live") or (task.get("source") or "") != "schedule":
        return False
    mode = (getattr(config, "PLAN_POSTS", "") or "digest").lower()
    kind = result.get("kind")
    if mode == "off" and kind != "digest":
        return True
    if mode == "digest" and kind not in REPORT_KINDS and not (result.get("problems") or []):
        return True                   # a plan with nothing wrong is read in the morning digest
    stage = result.get("stage") or task.get("workflow") or ""
    if mode == "digest" and kind not in REPORT_KINDS:
        # Say a problem when it is new, not every day: a decision still pending ("the heir caller is
        # not named") would otherwise post every morning until it is made. Counts inside a problem
        # change daily, so they are left out of the comparison.
        import hashlib
        import re
        shape = sorted({re.sub(r"\d+", "#", str(p)) for p in (result.get("problems") or [])})
        digest = hashlib.sha256("\n".join(shape).encode("utf-8")).hexdigest()[:16]
        key = "autopilot:plan_problems:%s" % stage
        if store.kv_get(key) == digest:
            return True
        store.kv_set(key, digest)
        return False
    try:
        tz = ZoneInfo(policy_mod.load().tz)
    except Exception:  # noqa: BLE001  (no playbook, or a zone the box does not know)
        tz = ZoneInfo("America/New_York")
    day = (now or datetime.now(timezone.utc)).astimezone(tz).date().isoformat()
    n = len(result.get("problems") or [])
    key = "autopilot:plan_said:%s" % stage
    try:
        prev = json.loads(store.kv_get(key) or "{}")
    except ValueError:
        prev = {}
    if prev.get("day") == day and n <= int(prev.get("problems") or 0):
        return True
    store.kv_set(key, json.dumps({"day": day, "problems": n}))
    return False


def slack_text(task: dict, result: dict, now: datetime | None = None) -> str:
    kind = result.get("kind")
    quiet = not (result.get("acted") or result.get("problems") or result.get("batch"))
    if quiet and kind not in REPORT_KINDS and not (task.get("source") or "").startswith("slack"):
        return ""
    if plan_repeat(task, result, now):
        return ""
    title = (result.get("stage") or task.get("workflow") or "").replace("_", " ")
    mode = "" if result.get("live") else " _(plan: nothing written)_"
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    out = ["%s *%s*%s %s" % (icon, title, mode, result.get("head") or "")]
    many = kind == "digest"          # the digest carries what the plan-mode stages would do
    for ln in (result.get("lines") or [])[:16 if many else 6]:
        out.append("  %s" % ln)
    for p in (result.get("problems") or [])[:6 if many else 3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)


# A stage whose problems are the team's to act on now (a new lead nobody has called), so they go to
# the pipeline channel as alerts, not to the issues channel as breakage.
ALERT_KINDS = {"leadclock"}
# Reports the team works from (the auction calendar, the day's numbers, the week's goals) go to the
# pipeline channel; the owner's reports (cadence, research results, tidiness, the ladder) stay in the
# team channel, where Ty talks to the team.
TEAM_REPORT_KINDS = {"auction", "capture", "goals", "callbacks", "contactrate", "coldstats"}
# Live stages that act on nearly every call. A run that did something posts no update for these: the
# attempt log runs every 15 minutes and would post about 40 times a day. Their problems still post.
QUIET_LIVE_KINDS = {"attemptlog", "primaryphone"}
# Stages that post for themselves: the KPI tracker's sinks keep ONE message a day edited in place and send
# the run's problems to the issues channel once a day (autopilot/kpisinks.py), so the sweep posts nothing.
OWN_POST_KINDS = {"kpitracker"}
# Live stages whose run IS a list for a person to work today: the 4:30 full-attempt list of the owners
# still short of a full attempt. On the board their section is the head and the whole list, replaced by
# every run, never one more line in a log of runs (which would show the head and drop the list).
LIST_KINDS = {"fullattempt"}


def posts(task: dict, result: dict, now: datetime | None = None) -> list[dict]:
    """What a SCHEDULED stage run posts once the channels are broken out (Ty, 2026-11-15), as
    [{"kind": "pipeline" | "issues" | "chat", "text": ...}] in the team's voice (agents/voice.py):
      - the morning digest: one post to the pipeline channel;
      - a report stage (REPORT_KINDS): its report, to the pipeline channel when the team works from it
        (TEAM_REPORT_KINDS), else to the team channel;
      - a lead that needs a call now (ALERT_KINDS): one alert each, once a day per lead;
      - any other problem: one issues post each, once a business day while it lasts;
      - what a LIVE stage did: one short update to the pipeline channel.
    Any other plan-mode stage posts nothing on its own: what it would do is read in the morning digest.
    AGENTS_PLAN_POSTS=off silences everything a plan-mode stage says except the digest.
    A run asked for in Slack keeps its whole answer in its thread (slack_text), never this."""
    from .. import voice
    out: list[dict] = []
    stage = result.get("stage") or task.get("workflow") or ""
    kind = result.get("kind")
    tid = task.get("id")
    problems = [str(p) for p in (result.get("problems") or [])]
    if kind in OWN_POST_KINDS:
        return []                      # it posts its own one message a day (and its problems) itself
    if (getattr(config, "PLAN_POSTS", "") or "digest").lower() == "off" and kind != "digest" and not result.get("live"):
        return []
    if kind == "digest":
        out.append({"kind": "pipeline", "text": voice.digest_text(result.get("head") or "", result.get("lines") or [], tid,
                                                                  problems=len(problems))})
        problems_kind = "issues"
    elif kind in REPORT_KINDS:
        if result.get("head") or result.get("lines"):     # a report with nothing in it (a quiet callback list) posts nothing
            out.append({"kind": "pipeline" if kind in TEAM_REPORT_KINDS else "chat",
                        "text": voice.report_text(stage, result.get("head") or "", result.get("lines") or [], tid)})
        problems_kind = "issues"
    elif kind in ALERT_KINDS:
        problems_kind = "pipeline"
    else:
        problems_kind = "issues"
    for a in [str(x) for x in (result.get("alerts") or [])]:
        if voice.once(stage, a):                              # each alert once ever, matched exactly
            out.append({"kind": "pipeline", "text": voice.alert_text(stage, a, tid)})
    for p in problems:
        if not voice.first_today(stage, p):
            continue
        text = voice.alert_text(stage, p, tid) if problems_kind == "pipeline" else voice.issue_text(stage, p, tid)
        out.append({"kind": problems_kind, "text": text})
    if kind not in REPORT_KINDS and kind not in QUIET_LIVE_KINDS and result.get("live") \
            and (result.get("acted") or result.get("batch")):
        out.append({"kind": "pipeline", "text": voice.update_text(stage, result.get("head") or "", result.get("lines") or [], tid)})
    return [p for p in out if (p.get("text") or "").strip()]


# ---------------------------------------------------------------------- the boards layout (2026-11-19)
# Where each kind sits on its department's board: what a person acts on first, then the day's reports,
# then what ran. A kind not listed sits after these, in name order.
BOARD_RANK = {
    # sales. The 4:30 full-attempt list is numbers to dial before the day ends, so it sits by the callbacks:
    # a long board cuts the sections at its end first, and unranked (500) the list was the first thing cut.
    "leadclock": 10, "callbacks": 20, "fullattempt": 25, "appointments": 30, "auction": 40, "auctiontasks": 45,
    "curative": 48, "leadcheck": 50, "leadtasks": 55, "audit": 60, "limits": 65, "capture": 70, "goals": 75, "alarms": 80,
    # marketing
    "textlane": 10, "coldreplies": 15, "ftmpipeline": 20, "mail": 30, "mailcadence": 35, "obituary": 40,
    "heirlane": 45, "agencyexport": 50, "coldtopup": 52, "agencyscore": 55, "top1000": 60, "contactrate": 70,
    "coldstats": 72, "cadence": 75, "revoke": 80,
    # ops
    "digest": 5, "researchqueue": 20, "screen": 30, "skipscreen": 35, "score": 40, "native_trace": 45, "retrace": 50, "handoff": 55,
    "complete": 60, "phonecleanup": 65, "ownerswap": 67, "taxwatch": 70, "taxsale": 72, "probate": 74, "attempts": 80,
    "watchdog": 85, "accountchecks": 90,
}
# A head that says the run left something for a person to release: its own post, so it is not missed.
WAITING_RX = re.compile(r"until a person releases|for a person to (?:approve|release)|waits? for (?:a person|you)|"
                        r"release batch \d+", re.I)


def _first_sentence(text: str, limit: int = 160) -> str:
    t = " ".join(str(text or "").split())
    cut = re.split(r"(?<=[.!?])\s+(?=[A-Z])", t, maxsplit=1)[0]
    return cut if len(cut) <= limit else cut[:limit - 3].rstrip(" ,;:") + "..."


def _stage_block(stage: str):
    try:
        pol = policy_mod.load()
        return pol.stages.get(stage) if pol is not None else None
    except Exception:  # noqa: BLE001  (a playbook that does not load must not stop the post)
        return None


def kind_and_block(stage: str) -> tuple[str, object]:
    """(kind, playbook block) of a stage, ("", None) when it is not one: how a workflow a stage queued
    finds the department it reports to (channels.task_department)."""
    st = _stage_block(stage)
    return (getattr(st, "kind", "") or "", st) if st is not None else ("", None)


def board_plan(task: dict, result: dict, now: datetime | None = None) -> dict:
    """What a SCHEDULED stage run says in the boards layout (Ty, 2026-11-19), as a plan the worker
    carries out (worker._deliver):
      dept      sales, marketing or ops (channels.department: the stage's `slack:` key, else its kind)
      section   this stage's part of today's board, or None: a report's head and lines; a live run that
                acted as one more entry ("9:02am Batch 8: 58 texts to 32 owners released"); a live run of a
                list kind (LIST_KINDS) as its head and its list, acted or not, so a run that finds nobody
                left takes the earlier list down; and the team's to-dos the run found (findings), each a
                line. A plan-mode stage with nothing to report, nothing done and nothing found has no
                section: the morning update carries it.
      setup     the settings a person has to make, kept on the ops board until the stage stops asking
                (an empty list clears this stage's)
      alerts    [{"text", "rule"}]: a seller to call now (rule "daily"), an appointment just set or a batch
                waiting for a person (rule "once"). Each its own post in the department's channel.
      issues    [{"key", "text"}]: breakage, one per CAUSE however many records it hit, once a business day
                per cause across every stage (six stages failing to read the same log are one problem)
      replies   [text]: detail for the board's thread (the morning update in full)
    AGENTS_PLAN_POSTS=off keeps a plan-mode stage off the board and out of #issues, as it does today."""
    from .. import channels, voice
    stage = result.get("stage") or task.get("workflow") or ""
    kind = result.get("kind") or ""
    live = bool(result.get("live"))
    block = _stage_block(stage)
    dept = channels.department(kind, stage, block)
    plan = {"dept": dept, "stage": stage, "title": voice.stage_name(stage), "rank": BOARD_RANK.get(kind, 500),
            "section": None, "setup": [], "alerts": [], "issues": [], "replies": []}
    if kind in OWN_POST_KINDS:
        plan["setup"] = None                               # it posts for itself; its settings stay as they are
        return plan
    if (getattr(config, "PLAN_POSTS", "") or "digest").lower() == "off" and kind != "digest" and not live:
        plan["setup"] = None
        return plan
    sorted_ = {"alert": [], "setup": [], "finding": [], "breakage": []}
    for p in [str(x) for x in (result.get("problems") or [])]:
        sorted_[channels.classify(p, kind)].append(p)
    plan["setup"] = sorted_["setup"]
    head = str(result.get("head") or "")
    lines = [str(x) for x in (result.get("lines") or []) if str(x).strip() and str(x).strip() != "..."]
    findings = sorted_["finding"]
    acted = live and (result.get("acted") or result.get("batch")) and kind not in QUIET_LIVE_KINDS
    section = None
    if kind == "digest":
        section = {"head": head, "lines": findings}
        full = "\n".join([head] + lines + (["Worth a look: " + f for f in findings] if findings else []))
        if full.strip():
            plan["replies"].append("Morning update in full:\n" + full)
    elif kind in REPORT_KINDS:
        hypothetical = not live and head.lstrip().startswith("Would ") and not findings
        if (head or lines or findings) and not hypothetical:     # "Would screen 6 calls" helps nobody act
            section = {"head": head, "lines": findings + lines}
    elif acted or (live and kind in LIST_KINDS):
        # A list kind's lines are what the run is for (fullattempt.py writes one per owner still short), so
        # its section carries them; any other live run that acted is one entry, its head's first sentence.
        section = ({"head": head, "lines": findings + lines} if kind in LIST_KINDS
                   else {"entry": _first_sentence(head), "lines": findings})
        if acted and WAITING_RX.search(head) and not re.search(r"\breleased\b", head, re.I):
            plan["alerts"].append({"text": _first_sentence(head, 300), "rule": "once"})
    elif findings:
        section = {"head": head, "lines": findings}
    plan["section"] = section
    for a in [str(x) for x in (result.get("alerts") or [])]:
        plan["alerts"].append({"text": a, "rule": "once"})          # an appointment set: said once, ever
    for a in sorted_["alert"]:
        plan["alerts"].append({"text": a, "rule": "daily"})         # a lead to call now: once a day while it waits
    plan["issues"] = [{"key": g["key"], "text": g["text"]} for g in channels.group(sorted_["breakage"])]
    return plan


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
