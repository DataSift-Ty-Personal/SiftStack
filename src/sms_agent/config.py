"""Configuration for the two-way SMS agent.

Everything is env-driven so the receiver can run on a small box (Fly/Render/
a VPS) without carrying repo secrets. Nothing here reaches out on import.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / ".env")

def _env(key: str, default: str = "") -> str:
    """Read an environment variable with surrounding whitespace stripped.

    A credential carrying a trailing carriage return is not a typo you can see.
    It cost most of a morning: Windows `print()` emits CRLF, the CR rode into a
    Fly secret, and `sk-ant-...\\r` is an illegal HTTP header, so the Anthropic
    SDK raised a bare "Connection error" on a box whose network was
    demonstrably fine. Every credential set the same way carried the same CR,
    silently, waiting to fail on first use.

    Every read below goes through here, so the whole class of problem is
    impossible regardless of how a value reached the environment.
    """
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else value

# ---------------------------------------------------------------- storage
DATA_DIR = Path(_env("SMS_AGENT_DATA_DIR", str(ROOT / "output" / "sms_agent")))
DB_PATH = Path(_env("SMS_AGENT_DB", str(DATA_DIR / "sms_agent.db")))

# ---------------------------------------------------------------- receiver
# Both smrtPhone and DataSift post here. Neither signs its payload, so the
# secret lives in the URL path and we additionally allowlist source IPs when
# SMS_AGENT_ALLOWED_IPS is set.
WEBHOOK_SECRET = _env("SMS_AGENT_WEBHOOK_SECRET", "")
# Run the worker loop inside the receiver process. Correct for a single-box
# deployment: SQLite has one writer, so splitting web and worker across two
# machines would mean two machines wanting the same volume.
INLINE_WORKER = _env("SMS_AGENT_INLINE_WORKER", "0") in ("1", "true", "True")
WORKER_INTERVAL = int(_env("SMS_AGENT_WORKER_INTERVAL", "20"))
# How often to poll the smrtPhone SMS log for replies the webhook never
# delivered. 0 disables. Five minutes is frequent enough that a missed lead is
# still fresh, and light enough not to hammer the log.
RECONCILE_INTERVAL = int(_env("SMS_AGENT_RECONCILE_INTERVAL", "300"))
ALLOWED_IPS = [x.strip() for x in _env("SMS_AGENT_ALLOWED_IPS", "").split(",") if x.strip()]
RECEIVER_HOST = _env("SMS_AGENT_HOST", "0.0.0.0")
RECEIVER_PORT = int(_env("SMS_AGENT_PORT", "8080"))

# ---------------------------------------------------------------- smrtPhone
# Two transports. The public API (`POST /sms/send`, header X-Auth-smrtPhone) is
# text-only, which is exactly what a reply is; it was the MMS/image path that
# had no API and forced the browser route on the original screenshot send.
# `session` drives the web app instead, for accounts where the API token is not
# usable. `auto` prefers the API and falls back to the session.
TRANSPORT = _env("SMS_AGENT_TRANSPORT", "auto").lower()  # auto | api | session
# Admin > API Tokens in the smrtPhone web app (phone.smrt.studio).
SMRTPHONE_API_KEY = _env("SMRTPHONE_API_KEY", "")
SMRTPHONE_BASE = _env("SMRTPHONE_BASE", "https://phone.smrt.studio")
# Playwright storage_state captured by _api/smrtphone_login.py.
SMRTPHONE_STATE_FILE = _env("SMRTPHONE_STATE_FILE", str(ROOT / "smrtphone_state.json"))
SESSION_HEADLESS = _env("SMS_AGENT_SESSION_HEADLESS", "1") not in ("0", "false", "False")
# Admin > Phone Numbers. JSON array of E.164 strings, or a path to a JSON file.
SMRTPHONE_NUMBERS_RAW = _env("SMRTPHONE_NUMBERS", "")
NUMBERS_FILE = Path(
    _env("SMRTPHONE_NUMBERS_FILE")
    or _env("SMS_AGENT_NUMBERS_FILE")
    or str(ROOT / "config" / "sms_numbers.json")
)

# ---------------------------------------------------------------- reisift
DEALROOM_API = Path(
    _env("DEALROOM_API_PATH", r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api")
)
SIFT_ACCOUNT = _env("SMS_AGENT_SIFT_ACCOUNT", "datasift-apikey")
SIFT_IMPERSONATE = _env("SMS_AGENT_SIFT_IMPERSONATE", "")  # e.g. ty+2@dataflik.com
# No-expiry Open API key. This is what lets the agent run on a cloud box with no
# Deal Room checkout on disk, and it cannot go stale mid-run the way a JWT does.
REISIFT_API_KEY = _env("REISIFT_API_KEY", "")

# ---------------------------------------------------------------- Slack
SLACK_WEBHOOK_URL = _env("SMS_AGENT_SLACK_WEBHOOK") or _env("SLACK_WEBHOOK_URL", "")

# ---------------------------------------------------------------- model
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
MODEL = _env("SMS_AGENT_MODEL", "claude-opus-5")
# Thinking is on by default on Opus 5 and counts against max_tokens, so leave
# real headroom or replies truncate mid-sentence.
MAX_TOKENS = int(_env("SMS_AGENT_MAX_TOKENS", "8000"))

# ---------------------------------------------------------------- policy
# Autonomy ladder. Phase 1 = classify + write only. Phase 2 = + escalate.
# Phase 3 = draft replies to Slack. Phase 4 = auto-send high-confidence only.
PHASE = int(_env("SMS_AGENT_PHASE", "1"))

DRY_RUN = _env("SMS_AGENT_DRY_RUN", "1") not in ("0", "false", "False", "")

# One reply per inbound, and a hard ceiling on turns before a human must take
# over. Six is the point where a real conversation has either produced a lead
# or is going nowhere.
MAX_AI_TURNS = int(_env("SMS_AGENT_MAX_TURNS", "6"))
# Below this the reply is drafted for approval instead of sent.
CONFIDENCE_FLOOR = float(_env("SMS_AGENT_CONFIDENCE_FLOOR", "0.80"))
# Recipient-local send window. A bot texting at 11pm is both a compliance
# problem and the clearest tell that it is a bot.
QUIET_START_HOUR = int(_env("SMS_AGENT_QUIET_START", "8"))
QUIET_END_HOUR = int(_env("SMS_AGENT_QUIET_END", "21"))

# Answer "who is this?" from the fixed template pool instead of paging a human.
# It is the most common reply to touch 1, the answer never varies, and the copy
# is reviewed rather than generated.
ANSWER_WHO = _env("SMS_AGENT_ANSWER_WHO", "1") not in ("0", "false", "no")

# Daily campaign window, Eastern (Ty, 2026-08-11). The recipient-local quiet
# hours above still apply on top: this is when WE work, that is when THEY may
# be texted, and a send needs both.
CAMPAIGN_TZ = _env("SMS_AGENT_CAMPAIGN_TZ", "America/New_York")
CAMPAIGN_START_HOUR = int(_env("SMS_AGENT_CAMPAIGN_START", "9"))
CAMPAIGN_END_HOUR = int(_env("SMS_AGENT_CAMPAIGN_END", "18"))
CAMPAIGN_ENABLED = _env("SMS_AGENT_CAMPAIGN", "0") not in ("0", "false", "no")
CAMPAIGN_DAILY_CAP = int(_env("SMS_AGENT_CAMPAIGN_DAILY_CAP", "0"))  # 0 = pool capacity
CAMPAIGN_DAYS = _env("SMS_AGENT_CAMPAIGN_DAYS", "0,1,2,3,4")  # Mon-Fri

# Whole days between one owner's touches. A follow-up goes out EVERY day to
# whoever is next in their own sequence (Ty, 2026-08-12), which is the skill's
# proven Mon/Tue/Wed rhythm. At 2 days most of the book sat parked: 86 people
# waiting and 5 eligible on a morning that should have carried ninety.
TOUCH_GAP_DAYS = int(_env("SMS_AGENT_TOUCH_GAP_DAYS", "1"))

# External heartbeat: how long without a worker pass before it is called dead.
HEARTBEAT_STALE_MINUTES = int(_env("SMS_AGENT_HEARTBEAT_STALE", "20"))
# Per-number daily send cap, well under the 10DLC ceiling.
DAILY_CAP_PER_NUMBER = int(_env("SMS_AGENT_DAILY_CAP", "25"))
# Minimum seconds between two sends from the SAME number. A real person does
# not fire three texts off one phone in a minute; carriers notice, and so do
# recipients. Ten minutes keeps each number's pattern human.
MIN_SEND_GAP_SECONDS = int(_env("SMS_AGENT_MIN_GAP", "600"))
# Randomised gap between consecutive sends ACROSS the whole pool. 52 messages
# at a 60-180s spread lands over roughly two hours rather than four minutes.
# Randomised, not fixed: an exact cadence is itself a machine signature.
SEND_SPACING_MIN = int(_env("SMS_AGENT_SPACING_MIN", "60"))
SEND_SPACING_MAX = int(_env("SMS_AGENT_SPACING_MAX", "180"))

# Tags we write. Prefixed so sequence conditions can exclude them and our own
# writes never re-trigger the sequences that called us.
TAG_PREFIX = "sys_"
TAG_AI_PAUSED = f"{TAG_PREFIX}ai_paused"
TAG_AI_HANDLED = f"{TAG_PREFIX}ai_handled"
TAG_ESCALATED = f"{TAG_PREFIX}escalated"
TAG_OPT_OUT = "Do Not Market"

# Identity. The thread is signed by the person ACTUALLY ASSIGNED to the record
# (the `assigned_to` uuid), so the name in the text is the name that calls.
# SENDER_NAME is only the fallback for an unassigned record. If both are empty
# the agent is told it has no name rather than being left to invent one.
SENDER_NAME = _env("SMS_AGENT_SENDER_NAME", "")
# uuid -> first name, for resolving `assigned_to`. Config file wins over the
# API lookup because reisift publishes no documented user endpoint.
SENDERS_FILE = Path(_env("SMS_AGENT_SENDERS_FILE", str(ROOT / "config" / "sms_senders.json")))
# We NEVER say a company name: a named company is litigation bait. The agent
# describes itself by locality instead, built from the record's own county/city.
LOCALITY_FALLBACK = _env("SMS_AGENT_LOCALITY", "")  # e.g. "Blount County"

# Slack is a handoff surface, not a feed. Only a reply a person needs to TAKE
# OVER gets posted; wrong numbers, opt-outs and stray inbounds are dispositioned
# silently and reported in the digest. Add intents here to widen it.
# A seller often sends one thought across two or three texts. Hold the
# escalation briefly so the burst arrives as ONE notification carrying the whole
# thing, instead of three posts each repeating the transcript.
ESCALATION_DEBOUNCE_SECONDS = int(_env("SMS_AGENT_ESCALATION_DEBOUNCE", "75"))
# After this many quiet days, a revived conversation earns a fresh top-level post.
ESCALATION_QUIET_DAYS = int(_env("SMS_AGENT_ESCALATION_QUIET_DAYS", "14"))
# Who owns a positive reply. Tagged in the post and assigned on the record.
HANDOFF_NAME = _env("SMS_AGENT_HANDOFF_NAME", "Adriana")
HANDOFF_SLACK_ID = _env("SMS_AGENT_HANDOFF_SLACK_ID", "")  # needs a bot token to look up
HANDOFF_ASSIGNEE_UUID = _env("SMS_AGENT_HANDOFF_ASSIGNEE", "")

ESCALATE_INTENTS = [
    x.strip().upper()
    for x in _env("SMS_AGENT_ESCALATE_INTENTS", "INTERESTED").split(",")
    if x.strip()
]


def senders() -> dict:
    """assigned_to uuid -> first name. Also accepts an email or a full name key."""
    if SENDERS_FILE.exists():
        data = json.loads(SENDERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


def number_pools() -> dict:
    """Sending numbers grouped by the caller who owns them.

    Owner binding matters because the thread is signed by the person assigned
    to the record. If a text signed "Adriana" goes out from one of Tinaa's
    numbers, a homeowner who calls it back reaches Tinaa's flow while the text
    said Adriana. Same person on the text and on the callback, or the whole
    identity story falls apart on the first return call.
    """
    raw = SMRTPHONE_NUMBERS_RAW.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [n.strip() for n in raw.split(",") if n.strip()]
        if isinstance(parsed, dict):
            return {k: [str(n) for n in v] for k, v in parsed.items() if not k.startswith("_")}
        if isinstance(parsed, list):
            return {"": [str(n) for n in parsed]}
    if NUMBERS_FILE.exists():
        data = json.loads(NUMBERS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if isinstance(data.get("pools"), dict):
                return {k: [str(n) for n in v] for k, v in data["pools"].items()}
            data = data.get("numbers", [])
        return {"": [str(n) for n in data]}
    return {}


def numbers() -> list[str]:
    """Every sending number, flattened, order stable."""
    seen, out = set(), []
    for pool in number_pools().values():
        for n in pool:
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def missing() -> list[str]:
    """Config gaps that would make the agent silently do nothing."""
    gaps = []
    if not WEBHOOK_SECRET:
        gaps.append("SMS_AGENT_WEBHOOK_SECRET (receiver would accept anonymous posts)")
    if not SMRTPHONE_API_KEY:
        gaps.append("SMRTPHONE_API_KEY (cannot send replies)")
    if not numbers():
        gaps.append("SMRTPHONE_NUMBERS / config/sms_numbers.json (no sending pool)")
    if PHASE >= 3 and not ANTHROPIC_API_KEY:
        gaps.append("ANTHROPIC_API_KEY (cannot generate replies)")
    if PHASE >= 2 and not SLACK_WEBHOOK_URL:
        gaps.append("SMS_AGENT_SLACK_WEBHOOK (cannot escalate to the prospector channel)")
    return gaps

# Keep the prospector channel to interested parties only (Ty, 2026-08-11).
# Opt-out bookkeeping and DNT notes were burying the leads.
SLACK_INTERESTED_ONLY = _env("SMS_AGENT_SLACK_INTERESTED_ONLY", "1") not in ("0", "false", "no")
