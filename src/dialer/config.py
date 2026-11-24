"""Settings, all from the environment: a Fly secret on the box, the repo .env on a workstation.

Read once into `S` by `reload()`. Tests call `reload({...})` with their own values, so nothing a test
does depends on what this machine's .env happens to hold (the texting selftest once reached the live
account that way). `load_dotenv()` is called only by the command line, never on import.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent

CALL_MODES = ("off", "test", "live")          # who may be dialed: nobody, the test numbers, anyone the rules allow
WRITEBACK_MODES = ("off", "dry", "live")      # what reaches DataSift: nothing, a log of what would, the writes
RECORD_MODES = ("off", "one_party", "all")    # one_party: never record a number in an all-party-consent state
AMD_MODES = ("off", "detect", "auto_drop")    # detect: label a machine; auto_drop: leave the voicemail by itself

# States whose wiretap law needs every party's consent to record a phone call. A call is recorded only
# when neither the number's state nor the owner's mailing state is on this list (DIALER_RECORD=one_party).
# Kept conservative on purpose: a state whose courts or the published surveys disagree is on it (DE, MI and
# OR appear on one list and not another), because a recording nobody may keep is worth less than none.
ALL_PARTY_STATES = ("CA", "CT", "DE", "FL", "IL", "MD", "MA", "MI", "MT", "NV", "NH", "OR", "PA", "WA")

# Voicemail drop plays a recorded message, and a recorded message to a homeowner is the TCPA's
# "prerecorded voice": courts let class claims proceed against agent-triggered drops (Newman v. Abstrakt,
# 2026) and treated prerecorded house-buying calls as advertisements (Helfrich v. Raven3, S.D.N.Y. 2023).
# So it is built and OFF until the owner turns it on with counsel's sign-off. A voicemail the caller
# speaks live is not prerecorded and is always allowed.
VM_DROP_MODES = ("off", "on")


def _env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return default if v is None else str(v).strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)) or default)
    except ValueError:
        return default


def _hours(text: str, default: tuple) -> tuple:
    """'09:00-19:00' -> (540, 1140) minutes after midnight."""
    try:
        a, b = text.split("-")
        def m(t):
            h, _, mm = t.strip().partition(":")
            return int(h) * 60 + int(mm or 0)
        lo, hi = m(a), m(b)
        return (lo, hi) if 0 <= lo < hi <= 24 * 60 else default
    except (ValueError, AttributeError):
        return default


def _numbers(text: str) -> tuple:
    out = []
    for part in (text or "").replace(";", ",").split(","):
        d = "".join(c for c in part if c.isdigit())
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        if len(d) == 10:
            out.append(d)
    return tuple(out)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "output" / "dialer")
    db_path: Path = field(default_factory=lambda: ROOT / "output" / "dialer" / "dialer.db")
    public_url: str = ""                 # https://siftstack-dialer.fly.dev, no trailing slash
    calls: str = "test"
    test_numbers: tuple = ()
    writeback: str = "dry"
    record: str = "one_party"
    amd: str = "off"
    vm_drop: str = "off"
    call_hours: tuple = (9 * 60, 19 * 60)      # the house rule (team/autopilot.yaml limits.call_hours)
    legal_hours: tuple = (8 * 60, 21 * 60)     # the federal floor; nothing lifts it
    business_tz: str = "America/New_York"
    ring_seconds: int = 25
    advance_seconds: int = 3
    numbers_per_day: int = 1                   # house limit: a number is not dialed twice in a day
    callback_grace_minutes: int = 120          # a number that called in may be called back outside the house hours (never the legal ones)
    preset_words: tuple = ("CALL", "CALLER QUEUE")   # the power dialer lists presets whose folder or title has one of these
    gate: dict = field(default_factory=dict)   # the playbook's gate block (registry_flag, callable_tiers, ...)
    local_presence: bool = False               # dial from a pool number in the homeowner's area code when there is one
    session_days: int = 14
    secret: str = ""                           # signs cookies and links; generated into the volume when unset
    # Twilio
    account_sid: str = ""
    auth_token: str = ""
    api_key_sid: str = ""
    api_key_secret: str = ""
    twiml_app_sid: str = ""
    webhook_key: str = ""                      # the SharedKey secret, when Twilio signs with HMAC-SHA256
    edge: str = ""                             # Twilio edge for the browser leg, e.g. ashburn
    validate_webhooks: bool = True
    # DataSift
    sift_key: str = ""
    sift_base: str = "https://apiv2.reisift.io"
    sift_gap_s: float = 1.2                    # pacing for background writes (interactive reads skip the line)
    app_base: str = "https://app.reisift.io"
    # The texting app (Smarter Contact lives there)
    texting_url: str = ""
    texting_token: str = ""
    handraise_token: str = ""                  # the bearer the texting app sends when it pushes a hand raise here
    # Reporting
    kpi_url: str = ""                          # the brain's /hooks/kpi/<secret>/<connection>
    slack_webhook: str = ""
    inbound_greeting: str = ("Sorry we missed you. Please leave your name and the best time to reach you, "
                             "and we will call you right back.")

    @property
    def twilio_ready(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.api_key_sid and self.api_key_secret
                    and self.twiml_app_sid)


S = Settings()


def reload(overrides: dict | None = None) -> Settings:
    """Build the settings from the environment, then apply `overrides` (tests)."""
    global S
    data = Path(_env("DIALER_DATA_DIR", str(ROOT / "output" / "dialer")))
    gate_raw = _env("DIALER_GATE", "")
    try:
        gate = json.loads(gate_raw) if gate_raw else {}
    except ValueError:
        gate = {}
    words = tuple(w.strip().upper() for w in _env("DIALER_PRESET_WORDS", "CALL,CALLER QUEUE").split(",") if w.strip())
    s = Settings(
        data_dir=data,
        db_path=Path(_env("DIALER_DB", str(data / "dialer.db"))),
        public_url=_env("DIALER_PUBLIC_URL", "").rstrip("/"),
        calls=_env("DIALER_CALLS", "test").lower(),
        test_numbers=_numbers(_env("DIALER_TEST_NUMBERS", "")),
        writeback=_env("DIALER_WRITEBACK", "dry").lower(),
        record=_env("DIALER_RECORD", "one_party").lower(),
        amd=_env("DIALER_AMD", "off").lower(),
        vm_drop=_env("DIALER_VM_DROP", "off").lower(),
        call_hours=_hours(_env("DIALER_CALL_HOURS", "09:00-19:00"), (9 * 60, 19 * 60)),
        legal_hours=_hours(_env("DIALER_LEGAL_HOURS", "08:00-21:00"), (8 * 60, 21 * 60)),
        business_tz=_env("DIALER_BUSINESS_TZ", "America/New_York"),
        ring_seconds=_int("DIALER_RING_SECONDS", 25),
        advance_seconds=_int("DIALER_ADVANCE_SECONDS", 3),
        numbers_per_day=_int("DIALER_NUMBER_DIALS_PER_DAY", 1),
        callback_grace_minutes=_int("DIALER_CALLBACK_GRACE_MINUTES", 120),
        preset_words=words,
        gate=gate if isinstance(gate, dict) else {},
        local_presence=_env("DIALER_LOCAL_PRESENCE", "0") in ("1", "true", "yes", "on"),
        session_days=_int("DIALER_SESSION_DAYS", 14),
        secret=_env("DIALER_SECRET", ""),
        account_sid=_env("TWILIO_ACCOUNT_SID", ""),
        auth_token=_env("TWILIO_AUTH_TOKEN", ""),
        api_key_sid=_env("TWILIO_API_KEY_SID", ""),
        api_key_secret=_env("TWILIO_API_KEY_SECRET", ""),
        twiml_app_sid=_env("TWILIO_TWIML_APP_SID", ""),
        webhook_key=_env("TWILIO_WEBHOOK_KEY", ""),
        edge=_env("TWILIO_EDGE", ""),
        validate_webhooks=_env("DIALER_VALIDATE_WEBHOOKS", "1") not in ("0", "false", "no", "off"),
        sift_key=_env("REISIFT_API_KEY", ""),
        sift_base=_env("DIALER_SIFT_BASE", "https://apiv2.reisift.io").rstrip("/"),
        sift_gap_s=float(_env("DIALER_SIFT_GAP_S", "1.2") or 1.2),
        app_base=_env("DIALER_APP_BASE", "https://app.reisift.io").rstrip("/"),
        texting_url=_env("DIALER_TEXTING_URL", "").rstrip("/"),
        texting_token=_env("DIALER_TEXTING_TOKEN", ""),
        handraise_token=_env("DIALER_HANDRAISE_TOKEN", ""),
        kpi_url=_env("DIALER_KPI_URL", ""),
        slack_webhook=_env("DIALER_SLACK_WEBHOOK", ""),
        inbound_greeting=_env("DIALER_INBOUND_GREETING", Settings.inbound_greeting),
    )
    for k, v in (overrides or {}).items():
        if not hasattr(s, k):
            raise KeyError("no setting %r" % k)
        setattr(s, k, v)
    S = s
    return s


def problems(s: Settings | None = None) -> list[str]:
    """What is missing or wrong, in plain words (the doctor and /stats print these)."""
    s = s or S
    out = []
    if s.calls not in CALL_MODES:
        out.append("DIALER_CALLS must be one of %s, not %r" % (", ".join(CALL_MODES), s.calls))
    if s.writeback not in WRITEBACK_MODES:
        out.append("DIALER_WRITEBACK must be one of %s, not %r" % (", ".join(WRITEBACK_MODES), s.writeback))
    if s.record not in RECORD_MODES:
        out.append("DIALER_RECORD must be one of %s, not %r" % (", ".join(RECORD_MODES), s.record))
    if s.amd not in AMD_MODES:
        out.append("DIALER_AMD must be one of %s, not %r" % (", ".join(AMD_MODES), s.amd))
    if s.vm_drop not in VM_DROP_MODES:
        out.append("DIALER_VM_DROP must be one of %s, not %r" % (", ".join(VM_DROP_MODES), s.vm_drop))
    if s.amd == "auto_drop" and s.vm_drop != "on":
        out.append("DIALER_AMD=auto_drop leaves a recorded voicemail, so it also needs DIALER_VM_DROP=on (and counsel's sign-off)")
    if not s.public_url.startswith("https://"):
        out.append("DIALER_PUBLIC_URL must be the app's https address: Twilio calls it back and signs the URL")
    if not s.twilio_ready:
        missing = [n for n, v in (("TWILIO_ACCOUNT_SID", s.account_sid), ("TWILIO_AUTH_TOKEN", s.auth_token),
                                  ("TWILIO_API_KEY_SID", s.api_key_sid), ("TWILIO_API_KEY_SECRET", s.api_key_secret),
                                  ("TWILIO_TWIML_APP_SID", s.twiml_app_sid)) if not v]
        out.append("Twilio is not set up (missing %s); run: python -m dialer.cli setup" % ", ".join(missing))
    if not s.sift_key:
        out.append("REISIFT_API_KEY is not set, so nothing can be read from or written to DataSift")
    if s.calls == "test" and not s.test_numbers:
        out.append("DIALER_CALLS=test with no DIALER_TEST_NUMBERS: nothing can be dialed yet")
    if s.call_hours[0] < s.legal_hours[0] or s.call_hours[1] > s.legal_hours[1]:
        out.append("DIALER_CALL_HOURS reaches outside the legal hours; the legal hours still apply")
    if s.texting_url and not s.texting_token:
        out.append("DIALER_TEXTING_URL is set without DIALER_TEXTING_TOKEN, so the texting app will refuse every call")
    return out


def secret_key() -> bytes:
    """The signing key: DIALER_SECRET, else one generated once into the volume (never in the repo)."""
    if S.secret:
        return S.secret.encode("utf-8")
    p = S.data_dir / "secret.key"
    if p.exists():
        return p.read_text(encoding="utf-8").strip().encode("utf-8")
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    p.write_text(key, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key.encode("utf-8")


def load_dotenv() -> None:
    """The repo .env into the environment, without overwriting what is already set (command line only)."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


reload()
