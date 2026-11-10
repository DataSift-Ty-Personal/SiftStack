"""Agents configuration. Every value comes from the environment (Fly secrets on the box, .env on a
workstation), so one image serves every install."""
from __future__ import annotations

import os
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

SLACK_WEBHOOK = os.environ.get("AGENTS_SLACK_WEBHOOK") or os.environ.get("SLACK_WEBHOOK_URL", "")
MODEL = os.environ.get("AGENTS_MODEL") or "claude-opus-5-5"
INLINE_WORKER = (os.environ.get("AGENTS_INLINE_WORKER") or "1") == "1"

# The status and board the Research agent's deep prospect hangs off.
DP_STATUS = os.environ.get("AGENTS_DP_STATUS") or "deep prospecting"
DP_BOARD = os.environ.get("AGENTS_DP_BOARD") or "Deep Prospecting"
DP_COLUMN = os.environ.get("AGENTS_DP_COLUMN") or "Check Relatives"


def live() -> bool:
    return MODE == "live"
