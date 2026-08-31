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
# The dispo program posts to its OWN channel. Buyer traffic and seller
# traffic are different audiences and different people act on them, so
# a single webhook would put 'a price went out to 156 buyers' in the
# seller text-leads channel. Falls back to the seller webhook, which is
# wrong-channel but not silent, and doctor() says so.
DISPO_SLACK_WEBHOOK_URL = _env("SMS_AGENT_DISPO_SLACK_WEBHOOK", "")

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

# How recently we must have recorded sending a text for its webhook echo to
# count as ours. Wide enough to clear webhook plus write latency (seconds, or
# under a minute across a worker restart), far under the one-per-day manual
# touch cadence, and matched to MIN_SEND_GAP_SECONDS so the window can never
# span two of our own sends to the same thread.
AUTHORSHIP_WINDOW_MINUTES = int(_env("SMS_AGENT_AUTHORSHIP_WINDOW", "10"))
# A call shorter than this is a voicemail or a wrong number, not a human taking
# over the conversation. Same threshold the call coaching pipeline already uses.
CALL_TAKEOVER_MIN_SECONDS = int(_env("SMS_AGENT_CALL_TAKEOVER_SECONDS", "60"))
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
# Post an alert when a day releases fewer than this. 0 disables it. Daily volume
# fell from 180 to 19 over four days in August and nothing said so; this is the
# line that would have caught it the first morning.
CAMPAIGN_MIN_EXPECTED = int(_env("SMS_AGENT_CAMPAIGN_MIN_EXPECTED", "0"))

# May we text a phone whose do-not-call flag we cannot see?
#
# The flag exists ONLY on a records-search row's representative phone.
# Verified 2026-08-31 against the full record, the owner endpoint, and a
# targeted search: the phone object carries number/type/status/tags/
# is_connected and nothing else, and searching a non-representative number
# returns the record's representative phone instead. So for any other number
# on a record the flag is not merely unread, it is unavailable.
#
# It is DataSift's registry scrub, not a person's opt-out: a real opt-out
# writes DNC / CORRECT_DNC / WRONG_DNC into the phone STATUS, which we do see
# on every phone and always honour. This setting governs only the scrub.
#
# Ty, 2026-08-31: "DNC is okay, but the litigation list here on sellers is what
# we'd want to suppress throughout the entire process." So this defaults OFF
# and the hard block is the litigator list below, which is the real exposure.
#
# 1: refuse a phone whose flag cannot be seen. Costs about 62 FTM candidates a
#    day, roughly 136 sends instead of 175.
# 0 (default): text the best phone on the record, honouring the phone status
#    and our own suppression, accepting that the registry scrub is invisible.
REQUIRE_VISIBLE_DNC = _env("SMS_AGENT_REQUIRE_VISIBLE_DNC", "0") not in ("0", "false", "no")

# TCPA serial-plaintiff suppression. Trestle returns
# `add_ons.litigator_checks["phone.is_litigator_risk"]` on the SAME call that
# returns line type, so one pass answers both. A hit is written into the local
# suppression table, which every send path already consults, so the block
# covers outreach, replies and any future program without new plumbing.
LITIGATOR_SUPPRESSION_REASON = "litigator"

# OUR OWN business hours, in one fixed timezone, applied to EVERY outbound
# message rather than only to the campaign build (Ty, 2026-08-28: "9 am to
# 6 pm Eastern from here on").
#
# This is a SECOND gate, not a replacement for recipient-local quiet hours.
# The two answer different questions and neither covers the other. Recipient
# local asks "is it a civil hour where they live", which is the compliance
# question and cannot be expressed in a fixed zone, because 9am Eastern is 6am
# in California. This one asks "are we open", so a reply never lands at an hour
# when nobody here can take the callback it invites. A send needs BOTH, and the
# queue waits for the overlap.
#
# Defaults follow the campaign window so there is one place to change the hours.
BUSINESS_TZ = _env("SMS_AGENT_BUSINESS_TZ", CAMPAIGN_TZ)
BUSINESS_START_HOUR = int(_env("SMS_AGENT_BUSINESS_START", str(CAMPAIGN_START_HOUR)))
BUSINESS_END_HOUR = int(_env("SMS_AGENT_BUSINESS_END", str(CAMPAIGN_END_HOUR)))
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

# PER-POOL overrides, e.g. {"Dispo": 35}. The cap is a compliance knob,
# and it used to be one global number: raising it so a 156-message dispo
# blast fits in one day would also have raised the acquisitions numbers
# from 25, which is a carrier-risk change to a different program nobody
# asked for. Pools that are not listed keep DAILY_CAP_PER_NUMBER.
def _pool_caps():
    import json as _json
    raw = _env("SMS_AGENT_POOL_CAPS", "")
    if not raw:
        return {}
    try:
        data = _json.loads(raw)
        return {str(k): int(v) for k, v in data.items()}
    except Exception:
        return {}


POOL_CAPS = _pool_caps()
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
# Marketing dispositions for sensitive replies (Ty, 2026-08-26). A grieving
# family telling us to go away does not need a Slack post; it needs the record
# marked so nobody contacts them again. The channel is for live sellers.
TAG_MAIL_ONLY = _env("SMS_AGENT_TAG_MAIL_ONLY", "Mail Only")
# Post a sensitive reply to Slack ONLY when it carries legal exposure (a lawyer,
# a regulator, a harassment claim, a minor). Everything else is dispositioned
# silently: suppressed, tagged, and noted on the record.
SENSITIVE_SLACK_LEGAL_ONLY = _env("SMS_AGENT_SENSITIVE_SLACK_LEGAL_ONLY", "1") not in ("0", "false", "False", "")

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
