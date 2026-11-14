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
# Smarter Contact (ConnectSI) DOES sign its webhooks: the account's whsec_
# secret from Connect > Webhooks. Its route skips the IP allowlist (built for
# smrtPhone) and is refused outright while this is unset. Retries keep their
# first timestamp for ~90 minutes, hence an age window of hours.
SC_SIGNING_SECRET = _env("SMARTERCONTACT_SIGNING_SECRET", "")
# The webhook Smarter Contact's own team configures (event SMS_MESSAGE_RECEIVED)
# authenticates with a header we chose instead: "Authorization: Bearer <this>".
# A token of its own, so nothing Smarter Contact holds opens the smrtPhone feed.
SC_WEBHOOK_TOKEN = _env("SMARTERCONTACT_WEBHOOK_TOKEN", "")
SC_SIGNATURE_MAX_AGE = int(_env("SMARTERCONTACT_SIGNATURE_MAX_AGE", str(3 * 60 * 60)))
# Replies and labels in Smarter Contact go through its web app's own API, signed
# in as Ty's account (no public API on this plan). The refresh token is the
# login: a password change or "sign out everywhere" kills it.
SC_FIREBASE_API_KEY = _env("SMARTERCONTACT_FIREBASE_API_KEY", "")
SC_REFRESH_TOKEN = _env("SMARTERCONTACT_REFRESH_TOKEN", "")
# The off switch. 0 = classify, label and page Adriana but never text anyone
# back from Smarter Contact. Flip to 1 only after real replies have arrived.
SC_AUTO_REPLY = _env("SMARTERCONTACT_AUTO_REPLY", "0") in ("1", "true", "True")
# Labels set in Smarter Contact (Not interested, Wrong Number, Unknown DNC). The
# DataSift integration turns each into a status or phone status, so they are a
# write to the CRM too and have their own switch.
SC_AUTO_LABEL = _env("SMARTERCONTACT_AUTO_LABEL", "0") in ("1", "true", "True")
# A reply that lands in seconds reads as a bot; one that lands in a couple of
# minutes reads as a person who saw the phone buzz.
SC_REPLY_DELAY_MIN = int(_env("SMARTERCONTACT_REPLY_DELAY_MIN", "90"))
SC_REPLY_DELAY_MAX = int(_env("SMARTERCONTACT_REPLY_DELAY_MAX", "240"))
# After this many auto replies on one thread, the next reply goes to a person.
# Three: who, how we got the number, and one clarify (Ty, 2026-10-30).
SC_MAX_AUTO_REPLIES = int(_env("SMARTERCONTACT_MAX_AUTO_REPLIES", "3"))
# A model read below this is left for a person ("This is Debbie. Yes we are.
# Do you have an issue with?" read ASKING_WHO at 0.50 on launch day).
SC_MIN_CONFIDENCE = float(_env("SMARTERCONTACT_MIN_CONFIDENCE", "0.65"))
# A "not interested" text sets the Sift status to Not Interested (Ty, 2026-10-28),
# the same disposition the Smarter Contact label maps to. Only on a record no one
# is working: a sold, contracted or live-lead record keeps its status and gets a note.
SC_NI_STATUS = _env("SMARTERCONTACT_NI_STATUS", "not_interested")
SC_NI_OVERWRITE = {s.strip().lower() for s in
                   _env("SMARTERCONTACT_NI_OVERWRITE_STATUSES", ",prospecting").split(",")}
# An interested reply naming a price above this multiple of Sift's value is not
# worth Adriana's call (Ty, 2026-10-30: "2 million" on a $599K house). It is
# dispositioned Not Interested instead of being pushed to the channel.
SC_PRICE_CEILING = float(_env("SMARTERCONTACT_PRICE_CEILING", "1.25"))
# A record whose MLS field reads one of these is on the market: not worth the time.
SC_LISTED_MLS = {s.strip().lower() for s in _env("SMARTERCONTACT_LISTED_MLS", "listed,pending,active").split(",")}
# Sift's MLS field lags: one house read "Off Market" while Zillow had it
# agent-listed at $750,000 for 58 days. Zillow is checked on every interested
# reply when this key is set (OpenWeb Ninja property-details-address).
ZILLOW_API_KEY = _env("OPENWEBNINJA_API_KEY", "")
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


# PER-POOL send gap, e.g. {"Dispo": 300}. Same reasoning as POOL_CAPS:
# pacing is a carrier-risk setting per program, and the dispo blast needs
# a tighter rest than acquisitions to clear its window without breaking
# the sticky-number rule. Pools not listed keep MIN_SEND_GAP_SECONDS.
def _pool_gaps():
    import json as _json
    raw = _env("SMS_AGENT_POOL_GAPS", "")
    if not raw:
        return {}
    try:
        return {str(k): int(v) for k, v in _json.loads(raw).items()}
    except Exception:
        return {}


POOL_GAPS = _pool_gaps()
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
# Every opt-out and legal hard stop also lands on this Sift list (Ty, 2026-10-28),
# so one list holds everyone who told us to stop.
LIST_DNC = _env("SMS_AGENT_DNC_LIST", "DNC")
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


# ---------------------------------------------------------------- build plan switches (2026-11-13)
#
# Every setting below belongs to the build plan (packages 0.1, 0.4, 0.14, 0.17, 0.22, 0.23, 2.2,
# 2.3 and 4.7) and every one of them defaults to what this app did the day before it was written.
# This app is live: a deploy with none of these set must behave exactly as the last deploy did.
# `SWITCHES` at the bottom is the same list in a form the CLI and the selftest can read.

def _flag(key: str, default: str = "0") -> bool:
    return _env(key, default).lower() in ("1", "true", "yes", "on")


def _choice(key: str, default: str, allowed: tuple) -> str:
    """One of a fixed set of words. An unknown word is the default, never a guess: a typo in a
    Fly secret must not switch a safety check into a mode nobody chose."""
    value = _env(key, default).lower()
    return value if value in allowed else default


def _int(key: str, default: int) -> int:
    try:
        return int(float(_env(key, str(default)) or default))
    except ValueError:
        return default


# 0.1 The suppression gate (gate_rules.py) in front of the seeder and the Smarter Contact loaders.
#   off      today: the seeder's own checks only.
#   report   the gate runs and says what it would hold; nothing is held because of it.
#   enforce  a record the gate refuses is held, with the gate's reason.
GATE_MODE = _choice("SMS_AGENT_GATE", "off", ("off", "report", "enforce"))
# Also read the record's custom fields (the Owner Deceased field). One more request a record.
GATE_READ_FIELDS = _flag("SMS_AGENT_GATE_READ_FIELDS")
# The same keys as the `gate:` block of the brain's playbook, as JSON, for an install whose tag
# or list names differ: {"working_statuses": ["My Status"], "deceased_tags": ["deceased"]}.
GATE_SETTINGS_RAW = _env("SMS_AGENT_GATE_SETTINGS", "")

# 0.17 A no stops what is already in motion.
# OWNER_REVOKE: an opt-out, a no, a hand raise or a reported death on one number pulls the queued
#   and held texts to every other number of that owner (and its Smarter Contact campaigns).
# SEND_RECHECK: the last look before each text leaves.
#   off    today: this number's own suppression and thread state.
#   local  plus the owner's other numbers, from this app's own store (no network).
#   full   plus the gate on the record as DataSift holds it right then (one read a send).
OWNER_REVOKE = _flag("SMS_AGENT_OWNER_REVOKE")
SEND_RECHECK = _choice("SMS_AGENT_SEND_RECHECK", "off", ("off", "local", "full"))

# 0.4 One table for every no (dispositions.py). Off: each texting path keeps its own handling.
DISPOSITION_TABLE = _flag("SMS_AGENT_DISPOSITION_TABLE")
# Where "not now" goes. follow_up is the status Smarter Contact's Nurture label already writes.
NOT_NOW_STATUS = _env("SMS_AGENT_NOT_NOW_STATUS", "follow_up")
# A reply that is probably the owner but never said so gets one confirmation call. The tag is how
# a caller's queue finds it; nothing in this app dials.
TAG_CONFIRM_CALL = _env("SMS_AGENT_TAG_CONFIRM_CALL", "Confirm By Call")
# A phone that reaches a relative of the owner (a valid contact on an estate, not a wrong number).
TAG_RELATIVE = _env("SMS_AGENT_TAG_RELATIVE", "Relative")
# Records with simply no number get their own tag, so Mail Only keeps one meaning: a person asked
# not to be called. This app never writes it; the name lives here so every tool spells it alike.
TAG_NO_NUMBER = _env("SMS_AGENT_TAG_NO_NUMBER", "No Number")
# Tag a record when a reply reports a death, so research picks it up (the gate's deceased_tags
# already hold "Verify Deceased"). Empty, the default, writes no tag: today's behavior.
TAG_DEATH_REPORTED = _env("SMS_AGENT_TAG_DEATH_REPORTED", "")

# The three Smarter Contact labels the plan adds, plus Nurture. They must EXIST in Smarter Contact
# before this goes on: a label the account does not have fails the write and pages the channel.
SC_EXTRA_LABELS = _flag("SMARTERCONTACT_EXTRA_LABELS")
SC_LABEL_CORRECT = _env("SMARTERCONTACT_LABEL_CORRECT", "Correct Number")
SC_LABEL_WRONG_DNC = _env("SMARTERCONTACT_LABEL_WRONG_DNC", "Wrong DNC")
SC_LABEL_RELATIVE = _env("SMARTERCONTACT_LABEL_RELATIVE", "Relative")
SC_LABEL_NURTURE = _env("SMARTERCONTACT_LABEL_NURTURE", "Nurture")

# 2.3 A hand raise becomes a New Lead. Off since 2026-08-11, because the status change fired a
# sequence that assigned the record to someone other than the person who was paged. Turn it on
# only once the alert and the Call New Lead sequence name the same person (plan 2.1).
HAND_RAISE_NEW_LEAD = _flag("SMS_AGENT_HAND_RAISE_NEW_LEAD")
NEW_LEAD_STATUS = _env("SMS_AGENT_NEW_LEAD_STATUS", "new_lead")
# Statuses a hand raise never overwrites and never treats as a lead: a person looks first.
NEW_LEAD_PROTECTED = {s.strip().lower() for s in _env(
    "SMS_AGENT_NEW_LEAD_PROTECTED",
    "sold,closed,close_out,dnc,opt_out,buyer,buyer_found,buyer_lost").split(",") if s.strip()}
TAG_REPLY_ON_LEAD = _env("SMS_AGENT_TAG_REPLY_ON_LEAD", "Reply On Lead")
# Decision D14 (not made): a reply asking well over value.
#   not_interested  today's rule (Ty, 2026-10-30).
#   cold_lead       passed as a Cold lead with an "ask over value" tag.
OVER_VALUE = _choice("SMS_AGENT_OVER_VALUE", "not_interested", ("not_interested", "cold_lead"))
COLD_LEAD_STATUS = _env("SMS_AGENT_COLD_LEAD_STATUS", "Cold Lead")
TAG_OVER_VALUE = _env("SMS_AGENT_TAG_OVER_VALUE", "Ask Over Value")
# An interested reply on a listed house. Empty = today's rule (Not Interested); a status name
# (follow_up) sends it to nurture instead.
LISTED_STATUS = _env("SMS_AGENT_LISTED_STATUS", "")

# 2.2 The text that says a call is coming, about three minutes after a hand raise.
#   off   today: an interested owner gets no text from the agent at all.
#   held  the text is staged and waits for a person to release it.
#   auto  the text goes out on its own, inside both send windows.
SPEED_TO_LEAD = _choice("SMS_AGENT_SPEED_TO_LEAD", "off", ("off", "held", "auto"))
SPEED_TO_LEAD_DELAY = _int("SMS_AGENT_SPEED_TO_LEAD_DELAY", 180)
# DataSift sequences whose webhook means "this record just became a New Lead" (a lead that came
# in by phone or web form, which this app would not otherwise hear about).
SPEED_TO_LEAD_SEQUENCES = [s.strip().lower() for s in _env(
    "SMS_AGENT_SPEED_TO_LEAD_SEQUENCES", "Call New Lead,New Lead").split(",") if s.strip()]
# Who makes the call the text promises. Defaults to the person the handoff pages.
LEAD_CALLER_NAME = _env("SMS_AGENT_LEAD_CALLER_NAME", "") or HANDOFF_NAME

# 0.14 This app's share of the account's DataSift request budget (ratebudget.py).
#   off    today: nothing is counted, and the client paces itself.
#   count  every request is counted and reported (the digest, /stats); nothing is delayed.
#          The step to take first, so the share is set from a measured week and not a guess.
#   on     the count is also the limit.
# 1, true and yes read as on, like every other switch here.
_rate_mode = _env("SMS_AGENT_RATE_BUDGET", "off").lower()
_rate_mode = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(_rate_mode, _rate_mode)
RATE_BUDGET_MODE = _rate_mode if _rate_mode in ("off", "count", "on") else "off"
RATE_BUDGET = RATE_BUDGET_MODE != "off"       # are this app's requests counted at all
RATE_ENFORCE = RATE_BUDGET_MODE == "on"       # does the count also delay a request
RATE_PER_MIN = _int("SMS_AGENT_RATE_PER_MIN", 10)
RATE_PER_HOUR = _int("SMS_AGENT_RATE_PER_HOUR", 0)        # 0 = sixty times the minute share
# While the first-to-market box makes its morning run, this app's scans drop to a trickle.
# Reply dispositions are never slowed by it. "off" disables the window.
RATE_QUIET = _env("SMS_AGENT_RATE_QUIET", "06:25-07:15")
RATE_QUIET_PER_MIN = _int("SMS_AGENT_RATE_QUIET_PER_MIN", 3)
RATE_MAX_WAIT = _int("SMS_AGENT_RATE_MAX_WAIT", 180)

# 0.23 The day's sent texts, for the brain's touch audit: GET /hooks/<secret>/touches.
TOUCH_FEED = _flag("SMS_AGENT_TOUCH_FEED")

# 4.7 Number health: a flagged sending number is rested, not replaced.
REST_FLAGGED = _flag("SMS_AGENT_REST_FLAGGED")
REST_DAYS = _int("SMS_AGENT_REST_DAYS", 5)
# Our own handset for the canary text. Nothing is staged without it.
CANARY_TO = _env("SMS_AGENT_CANARY_TO", "")

# (environment name, the default, what the default means, what turning it on changes).
SWITCHES = [
    ("SMS_AGENT_GATE", "off", "the seeder's own checks only",
     "report: the suppression gate says what it would hold. enforce: it holds them."),
    ("SMS_AGENT_GATE_READ_FIELDS", "0", "the gate reads a death from the owner flag, tags and lists",
     "also reads the Owner Deceased field (one more DataSift request a record)."),
    ("SMS_AGENT_OWNER_REVOKE", "0", "a stop cancels the texts queued to that one number",
     "a stop, a no, a hand raise or a reported death pulls every number of that owner."),
    ("SMS_AGENT_SEND_RECHECK", "off", "the worker checks this number's own suppression and thread",
     "local: also the owner's other numbers. full: also the gate on the live record."),
    ("SMS_AGENT_DISPOSITION_TABLE", "0", "each texting path handles a no its own way",
     "both paths use dispositions.py: a smrtPhone no sets Not Interested, not now goes to Follow Up."),
    ("SMARTERCONTACT_EXTRA_LABELS", "0", "Not interested, Wrong Number and Unknown DNC only",
     "adds Correct Number, Wrong DNC, Relative and Nurture (create them in Smarter Contact first)."),
    ("SMS_AGENT_HAND_RAISE_NEW_LEAD", "0", "an interested reply is tagged and assigned, status untouched",
     "an interested reply sets New Lead, never over an existing lead status."),
    ("SMS_AGENT_OVER_VALUE", "not_interested", "a reply asking over 1.25 times value is closed Not Interested",
     "cold_lead: it is passed as a Cold lead with an Ask Over Value tag (decision D14)."),
    ("SMS_AGENT_LISTED_STATUS", "", "an interested reply on a listed house is closed Not Interested",
     "a status name here (follow_up) sends it to nurture instead."),
    ("SMS_AGENT_SPEED_TO_LEAD", "off", "an interested owner gets no text from the agent",
     "held: a call-is-coming text is staged for release. auto: it sends about three minutes later."),
    ("SMS_AGENT_RATE_BUDGET", "off", "DataSift requests are paced only by the client's own half second gap",
     "count: every request is counted and reported, nothing is delayed. on: the count is also a limit "
     "of 10 a minute, shared by every process on this database."),
    ("SMS_AGENT_TOUCH_FEED", "0", "no route serves the day's sent texts",
     "GET /hooks/<secret>/touches returns them (record id, last four digits, time, system)."),
    ("SMS_AGENT_REST_FLAGGED", "0", "every pool number is used",
     "a number on the rest list sends no outreach and takes no new conversation until its rest ends."),
]
