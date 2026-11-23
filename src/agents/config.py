"""Agents configuration. Every value comes from the environment (Fly secrets on the box, .env on a
workstation), so one image serves every install."""
from __future__ import annotations

import os

import model_registry
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("AGENTS_DATA_DIR") or ROOT / "output" / "agents")
DB_PATH = Path(os.environ.get("AGENTS_DB") or DATA_DIR / "agents.db")
RUNS_DIR = DATA_DIR / "runs"

# Shared secret in the webhook URL path. smrtPhone-style vendors sign nothing, and DataSift's
# webhook registry takes only a name and a URL, so the path is the authentication.
WEBHOOK_SECRET = os.environ.get("AGENTS_WEBHOOK_SECRET", "")
# The secret path of the Slack relay (agents/relay.py): the jobs that posted to #first-to-market post
# here instead. Its own secret, so the URL that lives on three systems cannot also queue a workflow.
RELAY_SECRET = (os.environ.get("AGENTS_RELAY_SECRET") or "").strip()
PUBLIC_URL = (os.environ.get("AGENTS_PUBLIC_URL") or "").rstrip("/")

# dry: no vendor spend, no CRM writes; the research still runs and the pack still posts.
# free: as dry, except that an autopilot stage marked live in the playbook whose kind spends nothing
#       acts on its schedule (stage_live below). The first step past dry: live without spending.
# live: spends within the caps below and writes back to the record.
MODE = (os.environ.get("AGENTS_MODE") or "dry").strip().lower()
RUN_CAP_USD = _f("AGENTS_RUN_CAP_USD", 5.0)
DAILY_CAP_USD = _f("AGENTS_DAILY_CAP_USD", 25.0)
DEDUPE_HOURS = _f("AGENTS_DEDUPE_HOURS", 24.0)
# Research has its own daily budget, apart from the cap above. Every deep prospect, whoever queued it
# (store.RESEARCH_WORKFLOWS), is checked against this cap and booked to it; DAILY_CAP_USD counts
# everything else (the employees' turns and skills, the stages' data spend), never research. Until
# 2026-11-19 an unset research cap took the daily cap's figure and an employee turn was checked against
# the whole day: research spent $19.89 and a scoring stage $5.00 of the shared $25, and the Slack asks
# after that were refused with the team's own work at $5.
RESEARCH_DAILY_CAP_USD = _f("AGENTS_RESEARCH_DAILY_CAP_USD", 25.0)
# Held back inside each run for the final analysis, the pass that turns the research into who signs
# and who to call first. Without it a file with three heirs spent its whole cap on traces and
# scoring and then skipped the one step that makes the file usable.
FINAL_RESERVE_USD = _f("AGENTS_FINAL_RESERVE_USD", 1.0)

SLACK_WEBHOOK = os.environ.get("AGENTS_SLACK_WEBHOOK") or os.environ.get("SLACK_WEBHOOK_URL", "")
MODEL = model_registry.model("employees")   # the four employees; every job's model: src/model_registry.py
INLINE_WORKER = (os.environ.get("AGENTS_INLINE_WORKER") or "1") == "1"
# A second worker thread for what a person or an event started (store.CLAIM_LANES); "0" goes back to
# one worker for everything.
PEOPLE_WORKER = (os.environ.get("AGENTS_PEOPLE_WORKER") or "1") == "1"
# The clock (scheduler.py) fires every live workflow with a schedule trigger. On by default in the
# brain; AGENTS_SCHEDULER=0 stops it without stopping the worker.
SCHEDULER = (os.environ.get("AGENTS_SCHEDULER") or "1") == "1"

# The team (team/roster.yaml): who works here, what each one owns, and the skills they carry.
TEAM_DIR = Path(os.environ.get("AGENTS_TEAM_DIR") or ROOT / "team")
SKILLS_DIR = Path(os.environ.get("AGENTS_SKILLS_DIR") or ROOT / "skills")
# Each employee runs in its own workspace OUTSIDE the repo, so the repo's CLAUDE.md and the
# operator's personal Claude settings never load into an employee's context. /data/workspaces on Fly.
WORKSPACES_DIR = Path(os.environ.get("AGENTS_WORKSPACES_DIR")
                      or Path(tempfile.gettempdir()) / "siftstack-agents" / "workspaces")

# Two-way Slack (a real app, not the post-only webhook). Without a bot token the team falls back
# to SLACK_WEBHOOK: it can still report, but it cannot thread, speak as each employee, or hear.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
# Socket Mode: an app-level token (xapp-..., scope connections:write). Slack pushes events over a
# connection the brain opens, so no Request URL has to be verified and no signing secret is needed.
# Either way in works; with both set, an event that arrives twice is dropped by its id.
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
# Slack user ids (U0...) allowed to release a staged batch from the channel (agents/release.py).
# Empty, nobody can: a batch is then released from the command line only.
RELEASERS = os.environ.get("AGENTS_RELEASERS", "")
# What a stage in plan mode posts on its own. digest (default): nothing but a new problem; what each
# stage would do is summarized in the morning digest. daily: its plan once a business day. off:
# nothing at all. With about 45 stages, daily would put dozens of posts in the channel every day.
PLAN_POSTS = (os.environ.get("AGENTS_PLAN_POSTS") or "digest").strip().lower()
# One channel for the whole team: a channel ID (C0...). Set, every post that belongs to no thread
# goes there whoever on the team is speaking, and the per-employee channel names in the roster are
# not needed. Unset, each employee posts in its own channel and the team shares one.
SLACK_CHANNEL = (os.environ.get("AGENTS_SLACK_CHANNEL") or "").strip()
# Channels by purpose (Ty, 2026-11-15: "have the channels broken out", and "a channel that has bugs or
# issues ... I don't want to clog up the actual pipeline that the team is using"). SLACK_CHANNEL stays
# the place to talk to the team. PIPELINE takes the morning digest, what a live stage did and a lead
# that needs a call now; ISSUES takes anything broken, once a day per problem, in plain words. An
# unset one falls back to SLACK_CHANNEL, so nothing is lost before the channels exist.
SLACK_PIPELINE_CHANNEL = (os.environ.get("AGENTS_SLACK_PIPELINE_CHANNEL") or "").strip()
SLACK_ISSUES_CHANNEL = (os.environ.get("AGENTS_SLACK_ISSUES_CHANNEL") or "").strip()
# Channels by department (Ty, 2026-11-19: "segment the notifications ... like a marketing, a sales, and
# ops channel", every employee still in each). Sales: leads, callers, appointments. Marketing: texts,
# mail, the agency, the lists. Ops: county data, research, scoring, the account's upkeep. An unset one
# falls back to the ops channel, then the pipeline channel, then the team channel (agents/channels.py).
SLACK_SALES_CHANNEL = (os.environ.get("AGENTS_SLACK_SALES_CHANNEL") or "").strip()
SLACK_MARKETING_CHANNEL = (os.environ.get("AGENTS_SLACK_MARKETING_CHANNEL") or "").strip()
SLACK_OPS_CHANNEL = (os.environ.get("AGENTS_SLACK_OPS_CHANNEL") or "").strip()
# How scheduled work reaches Slack. classic: one post per stage run, as it was until 2026-11-19.
# boards: one message per department a day that the team edits in place as it works (agents/board.py),
# a new post only for what a person has to act on now, and #issues for breakage alone, one post per
# cause however many records it hit.
SLACK_LAYOUT = (os.environ.get("AGENTS_SLACK_LAYOUT") or "classic").strip().lower()
# How a routed post is written (agents/voice.py). ty (default): a small model rewrites it the way Ty
# writes to his team, short and plain, an issue at a third-grade reading level; it may only move words,
# never add a number. plain: the free cleanup alone (no emoji, no programmer text, no "lead(s)").
SLACK_VOICE = (os.environ.get("AGENTS_SLACK_VOICE") or "ty").strip().lower()
SLACK_VOICE_MODEL = model_registry.model("slack_posts")
SLACK_VOICE_DAILY = int(_f("AGENTS_SLACK_VOICE_DAILY", 400))   # rewrites a day; past it, the cleanup alone
# Files dropped in Slack (walkthrough photos, a KML or CSV boundary, a PDF). Saved into the working
# employee's own workspace at the start of its turn (agents/attachments.py); the app needs files:read.
# A message with files starts its turn FILE_SETTLE_S later, and every further file message on the same
# thread pushes the start back, so a walkthrough posted ten photos at a time becomes ONE turn.
FILE_SETTLE_S = _f("AGENTS_FILE_SETTLE_S", 40.0)
MAX_FILES = int(_f("AGENTS_MAX_FILES", 60))              # files saved for one thread
MAX_FILE_MB = _f("AGENTS_MAX_FILE_MB", 20.0)             # a larger file is listed, not saved
FILES_KEEP_DAYS = _f("AGENTS_FILES_KEEP_DAYS", 14.0)     # saved files are deleted after this
FILE_TURN_USD = _f("AGENTS_FILE_TURN_USD", 8.0)          # turn cap when the turn carries files (a rehab skill run)

# Claude for conversational asks. On whenever a key exists; AGENTS_USE_LLM=0 forces the stub, which
# answers honestly without spending. Meet-the-team never uses the model, so it is the free smoke test.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_LLM = (os.environ.get("AGENTS_USE_LLM") or ("1" if ANTHROPIC_API_KEY else "0")) == "1"
# A skill worked end to end (read SKILL.md and its references, gather, build the JSON, run its generator,
# publish) needs more room than a three-line answer (2026-11-17); the day cap stays the ceiling.
AGENT_BUDGET_USD = _f("AGENTS_AGENT_BUDGET_USD", 5.0)     # Claude spend cap for one employee turn
AGENT_MAX_TURNS = int(_f("AGENTS_AGENT_MAX_TURNS", 50))
MAX_HOPS = int(_f("AGENTS_MAX_HOPS", 6))                  # handoffs allowed on one thread

# The status and board the Research agent's deep prospect hangs off.
DP_STATUS = os.environ.get("AGENTS_DP_STATUS") or "deep prospecting"
DP_BOARD = os.environ.get("AGENTS_DP_BOARD") or "Deep Prospecting"
DP_COLUMN = os.environ.get("AGENTS_DP_COLUMN") or "Check Relatives"


def live() -> bool:
    return MODE == "live"


# Stages that may act on a free box whatever their kind spends (Ty, 2026-11-18: the first-to-market
# texts and the foreclosure research go live while everything else stays free). A comma list of
# playbook stage ids; empty, free mode is exactly what it was. A workflow task one of these stages
# queued runs live as well (task_live); the research a webhook or a Slack ask starts stays dry.
LIVE_STAGES = frozenset(s.strip() for s in (os.environ.get("AGENTS_LIVE_STAGES") or "").split(",") if s.strip())


def stage_live(spend: str, stage_id: str = "") -> bool:
    """May an autopilot stage that the playbook marks live act on this box? On a live box, any such
    stage. On a box in AGENTS_MODE=free, one whose kind spends nothing (spend class "free": tags,
    statuses, counters, notes, tasks; no vendor, no text, no letter), or one named in
    AGENTS_LIVE_STAGES. Everything else on a free box stays as on a dry one: every workflow and the
    research a webhook starts. A staged batch is released where its stage would act by this same
    rule (agents/release.py), and a script command waits for a live box."""
    if MODE == "live":
        return True
    return MODE == "free" and (spend == "free" or bool(stage_id and stage_id in LIVE_STAGES))


def task_live(task: dict | None) -> bool:
    """May this workflow task spend and write back? On a live box, yes. On a free box, only a task a
    stage named in AGENTS_LIVE_STAGES queued (its source reads "autopilot:<stage id>"), so a record a
    person moves to Deep Prospecting, or an ask in Slack, still runs dry there."""
    if MODE == "live":
        return True
    if MODE != "free" or not task:
        return False
    source = str(task.get("source") or "")
    return source.startswith("autopilot:") and source.split(":", 1)[1] in LIVE_STAGES
