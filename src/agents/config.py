"""Agents configuration. Every value comes from the environment (Fly secrets on the box, .env on a
workstation), so one image serves every install."""
from __future__ import annotations

import os
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
PUBLIC_URL = (os.environ.get("AGENTS_PUBLIC_URL") or "").rstrip("/")

# dry: no vendor spend, no CRM writes; the research still runs and the pack still posts.
# live: spends within the caps below and writes back to the record.
MODE = (os.environ.get("AGENTS_MODE") or "dry").strip().lower()
RUN_CAP_USD = _f("AGENTS_RUN_CAP_USD", 5.0)
DAILY_CAP_USD = _f("AGENTS_DAILY_CAP_USD", 25.0)
DEDUPE_HOURS = _f("AGENTS_DEDUPE_HOURS", 24.0)
# Research (the deep prospect) has its own daily budget, apart from data spend and mail. Unset, it
# is the same figure as the daily cap, counted separately.
RESEARCH_DAILY_CAP_USD = _f("AGENTS_RESEARCH_DAILY_CAP_USD", DAILY_CAP_USD)
# Held back inside each run for the final analysis, the pass that turns the research into who signs
# and who to call first. Without it a file with three heirs spent its whole cap on traces and
# scoring and then skipped the one step that makes the file usable.
FINAL_RESERVE_USD = _f("AGENTS_FINAL_RESERVE_USD", 1.0)

SLACK_WEBHOOK = os.environ.get("AGENTS_SLACK_WEBHOOK") or os.environ.get("SLACK_WEBHOOK_URL", "")
MODEL = os.environ.get("AGENTS_MODEL") or "claude-opus-5-5"
INLINE_WORKER = (os.environ.get("AGENTS_INLINE_WORKER") or "1") == "1"
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

# Claude for conversational asks. On whenever a key exists; AGENTS_USE_LLM=0 forces the stub, which
# answers honestly without spending. Meet-the-team never uses the model, so it is the free smoke test.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
USE_LLM = (os.environ.get("AGENTS_USE_LLM") or ("1" if ANTHROPIC_API_KEY else "0")) == "1"
AGENT_BUDGET_USD = _f("AGENTS_AGENT_BUDGET_USD", 2.0)     # Claude spend cap for one employee turn
AGENT_MAX_TURNS = int(_f("AGENTS_AGENT_MAX_TURNS", 30))
MAX_HOPS = int(_f("AGENTS_MAX_HOPS", 6))                  # handoffs allowed on one thread

# The status and board the Research agent's deep prospect hangs off.
DP_STATUS = os.environ.get("AGENTS_DP_STATUS") or "deep prospecting"
DP_BOARD = os.environ.get("AGENTS_DP_BOARD") or "Deep Prospecting"
DP_COLUMN = os.environ.get("AGENTS_DP_COLUMN") or "Check Relatives"


def live() -> bool:
    return MODE == "live"
