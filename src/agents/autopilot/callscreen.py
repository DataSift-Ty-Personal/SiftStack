"""The evening call screen: every conversation read for four kinds of trouble, each one proven by a
quote from the call (Ty, 2026-11-15).

Why it exists. The coaching picks choose two best and two worst calls a week by length and outcome,
and the coaching workflow grades only those. Nothing listened to the rest, and nothing read a Not
Interested or a Dead Lead against the call it followed. The lessons this screen keeps:
  * C9.39: never give a price on the phone, not even a ballpark, before seeing the house;
  * C8.12: pass on anything that resembles a yes;
  * C9.37 with L10.37 and L10.38: Not Interested only once the seller has really said no, Dead only
    when truly dead; a lead that goes quiet, or a contract that falls apart, means an objection
    nobody uncovered (the price, the timing, another person who has a say, or trust);
  * A13.1: Claude reads every call, and people listen again only to the flagged ones.

Each evening it takes every answered call of `min_seconds` or more since its last run (inbound and
outbound), and the last calls of every lead that went quiet (Ghosting Lead) or whose contract fell
apart since then, and asks Claude Haiku 5.5 (thinking off) one question per call, about only the
checks that apply to it:
  (a) price: OUR side gave a price, a range, a ballpark or a formula for one;
  (b) soft yes: the seller showed interest. Whether anything FOLLOWED is not the model's call: the
      code reads the owner's houses after the call (a lead status, an open task, a task made after
      the call). Only a soft yes with none of those is flagged, and live mode gives the record's
      assignee one follow-up task for it, never a text and never a call;
  (c) a no that was not a refusal: asked only when the code found Not Interested or Dead Lead set on
      one of the owner's houses within `disposition_hours` of the call;
  (d) the missed objection: asked only of a quiet or fallen lead's last `quiet_calls` calls.

Rules, each from what goes wrong without it:
  * NO QUOTE, NO FLAG. Every flag carries words the code finds in the transcript, whole words and in
    the right speaker's lines: a price must be in OUR lines and carry a number; a soft yes, a non
    refusal and an objection must be in the SELLER's lines. A quote that is the other speaker's, a
    quote that is not there, and a "not a refusal" whose quote is itself a refusal are dropped and
    counted. A coaching flag that misquotes a caller costs the screen every caller's trust.
  * PAID ONCE. The transcripts lead notes and call coaching already bought are read as they are; a
    missing one is bought only inside `max_transcriptions` a run and this stage's own dollars a day
    (daily_usd), and it is kept in lead notes' own store so no stage ever pays for that call again.
  * A CALL IS SCREENED ONCE, AFTER IT SETTLES. A call younger than `settle_minutes` waits for the
    next evening: its soft yes gets time for a follow-up and lead notes time to transcribe it.
  * People only: a call on a house a company or trust owns is not screened.

Plan mode opens no recording, asks no model and spends nothing: it says how many calls it would
screen, how many transcripts are on hand and what the rest would cost. Its marks are its own.

Privacy: transcripts stay in the brain's data folder. The report names the street, the caller's
first name and the quote, with any phone number and the owner's names taken out; never the
recording link. Flags are kept in the brain's own table (ap_callscreen) for the coaching that
follows them.
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
import model_registry
from . import activity, budget, kpi, lanes, ledger, leadnotes
from . import leadroutes as routes
from .attemptlog import call_kind, transcript_of, turns
from .stages import addr, assignee_of

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KIND = "callscreen"
KEY, PLAN_KEY = "callscreen", "callscreen_plan"
TASK_ACTION = "callscreen_task"
FINAL = ("screened", "skipped")
CHECKS = ("price", "soft_yes", "no_refusal", "missed_objection")
LABELS = {"price": "price on the phone", "soft_yes": "soft yes with no next step",
          "no_refusal": "no that was not a refusal", "missed_objection": "objection nobody asked about"}
OBJECTIONS = {"price": "the price", "time": "the timing", "third party": "another person who has a say",
              "trust": "trust in us", "other": "a concern"}
CALL_EVENTS = ("owner.call.answered", "owner.call.received")
# A quote that says one of these IS a refusal, whatever the model made of the rest of the call.
REFUSALS = ("not interested", "no thank you", "no thanks", "not selling", "not for sale", "don't call", "do not call",
            "stop calling", "take me off", "take us off", "remove me", "remove my number", "leave me alone",
            "lose my number", "never sell", "won't sell", "not going to sell", "not gonna sell")
DEFAULTS = {
    "min_seconds": 60,              # a shorter answered call is a greeting or a voicemail
    "call_events": list(CALL_EVENTS),
    "first_hours": 24,              # how far back the first run reaches, before the settle time
    "settle_minutes": 120,          # a call this fresh is screened the next evening
    "max_age_days": 7,              # a call still not screened after this long (a cap, a missing key) is closed
    "max_calls": 60,                # model calls a run (one a call)
    "max_reads": 120,               # DataSift reads a run
    "transcribe": True,             # buy a missing transcript, inside the caps
    "max_transcriptions": 8,        # a run
    "max_minutes": 30,              # a longer recording is not sent
    "max_mb": 40,
    "audio_per_min": 0.003,         # estimates for the caps; what OpenRouter and Claude bill is what is booked
    "screen_usd": 0.002,
    "model": model_registry.model("call_review"),
    "max_tokens": 1500,
    "max_tries": 3,
    "recording_grace_hours": 24,    # a recording gone this soon after the call is tried again
    "min_quote_words": 3,           # a quote shorter than this proves nothing
    "disposition_hours": 12,        # Not Interested or Dead set this soon after a call is that call's decision
    "no_statuses": ["not_interested", "Dead Lead"],
    "quiet_statuses": ["Ghosting Lead"],
    "lost_statuses": ["lost_deal"],
    "contract_statuses": ["under_contract", "transactions"],
    "closed_statuses": ["closed", "close_out", "sold"],
    "quiet_calls": 2,               # the last calls read for a quiet lead's missed objection
    "quiet_days": 45,               # how far back those calls may be
    "lead_statuses": list(lanes.LEAD_STATUS_VALUES),
    "follow_up_task": True,         # live: one task for the assignee on a soft yes nothing followed
    "task_due_days": 1,             # business days until that task is due
    "max_flag_lines": 10,
    "sync": True,
    "sync_calls": 60,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_callscreen (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, day TEXT, live INTEGER, stage TEXT, call_id TEXT, owner TEXT,
    record TEXT, street TEXT, caller TEXT, call_at TEXT, kind TEXT, detail TEXT, quote TEXT, task TEXT DEFAULT '');
"""

# Stand-ins the selftest sets, so no test can reach a dialer, OpenRouter or Claude. None = the real one.
FETCH = None        # (url, max_bytes) -> a dict shaped like call_coaching.transcribe.fetch_recording's
TRANSCRIBE = None   # (audio, fmt, caller) -> (transcript, usage)
ASK = None          # (system, user, model, max_tokens) -> (reply text, usage, stop reason)

EM, EN = chr(0x2014), chr(0x2013)
_ELLIPSIS_RX = re.compile(r"\.\.\.|" + chr(0x2026))
_PHONE_RX = re.compile(r"(?<![\d$])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SEVEN_RX = re.compile(r"(?<![\d$,.])\b\d{3}[\s.-]\d{4}\b")


# ---------------------------------------------------------------------- the prompt
SYSTEM = """You screen one phone call for the owner of a real estate investing team. In the transcript, AGENT is
our team member (AGENT_VM is a voicemail our team member leaves), SELLER is the owner of the house or
whoever answered for them, and VOICEMAIL is an automated greeting.

The one rule that matters: every finding must be backed by a quote copied word for word from the
transcript, without the speaker label and without bracketed notes such as [pause]. If you cannot quote
it, the finding is false. Never guess, and never fill a gap from what usually happens on calls like this.

The checks (answer only the ones you are asked for):
- price: did the AGENT give a price, a price range, a ballpark, or a way to work the price out (such as a
  percent of what the house is worth)? The seller naming their own price does not count. Quote the AGENT's
  words that give it.
- soft_yes: did the SELLER show any interest: a maybe, a call me back, a question about the offer or the
  process, a number they would take, a time they might sell? Quote the SELLER's words.
- no_refusal: the record was marked not interested or dead after this call. Found is true only when the
  SELLER never actually refused. Quote the SELLER's words that show what they really said (asking for
  time, a question, a condition). If the seller said no, found is false.
- missed_objection: this lead later went quiet or its contract fell apart. Found is true only when the
  SELLER hinted at a concern the AGENT never asked about or answered. Name it as price, time, third party
  (a spouse, a relative, a lawyer, an heir) or trust, and quote the SELLER's words.

Answer with one JSON object and nothing else, one key per check asked, for example:
{"price": {"found": false, "quote": ""}, "missed_objection": {"found": true, "objection": "third party", "quote": "..."}}"""


def user_text(transcript: str, checks: list[str], context: list[str], max_chars: int = 60000) -> str:
    lines = ["Checks for this call: %s." % ", ".join(checks)]
    for c in context:
        lines.append("What happened after the call: %s." % c)
    lines += ["", "TRANSCRIPT:", str(transcript or "")[:max_chars]]
    return "\n".join(lines)


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with a call screen block, for the playbook checker."""
    out = []
    for key, low in (("min_seconds", 1), ("max_calls", 0), ("max_reads", 1), ("max_transcriptions", 0), ("max_tokens", 300),
                     ("max_tries", 1), ("min_quote_words", 2), ("quiet_calls", 0), ("max_flag_lines", 1), ("sync_calls", 1),
                     ("task_due_days", 0)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    for key in ("first_hours", "settle_minutes", "max_age_days", "max_minutes", "max_mb", "audio_per_min", "screen_usd",
                "recording_grace_hours", "disposition_hours", "quiet_days"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a number, 0 or more" % (st.id, key))
    ev = st.get("call_events")
    if ev is not None and (not isinstance(ev, list) or not ev or [x for x in ev if str(x) not in CALL_EVENTS]):
        out.append("stage %s: call_events lists call events from %s" % (st.id, ", ".join(CALL_EVENTS)))
    for key in ("no_statuses", "quiet_statuses", "lost_statuses", "contract_statuses", "closed_statuses", "lead_statuses"):
        v = st.get(key)
        if v is not None and (not isinstance(v, list) or [x for x in v if not str(x or "").strip()]):
            out.append("stage %s: %s lists statuses, as the account stores them" % (st.id, key))
    if st.get("model") is not None and not str(st.get("model") or "").strip():
        out.append("stage %s: model may not be empty" % st.id)
    for key in ("transcribe", "follow_up_task", "sync"):
        if st.get(key) is not None and not isinstance(st.get(key), bool):
            out.append("stage %s: %s is true or false" % (st.id, key))
    return out


# ---------------------------------------------------------------------- what the call said
def _lows(items) -> set[str]:
    return {str(x).strip().lower() for x in items or [] if str(x).strip()}


def speakers(transcript: str) -> dict:
    """Each speaker's turns, read as words only: {"agent": [...], "seller": [...], "all": [...]}. A
    quote is matched inside ONE turn: joining a speaker's turns would let two halves said minutes apart
    pass as one sentence."""
    out = {"agent": [], "seller": [], "all": []}
    for label, said in turns(transcript):
        t = leadnotes.normalize(said)
        if not t:
            continue
        out["all"].append(t)
        if label in ("AGENT", "AGENT_VM"):
            out["agent"].append(t)
        elif label == "SELLER":
            out["seller"].append(t)
    return out


def quoted(quote: str, said: list[str], min_words: int = 3) -> bool:
    """Is this quote in these turns, as whole words? An ellipsis joins pieces, and every piece must be
    in a turn and at least `min_words` long: "yes" is in every call and proves nothing."""
    pieces = [leadnotes.normalize(p) for p in _ELLIPSIS_RX.split(quote or "")]
    pieces = [p for p in pieces if p]
    if not pieces:
        return False
    return all(len(p.split()) >= min_words and any((" %s " % p) in (" %s " % t) for t in said) for p in pieces)


def has_number(quote: str) -> bool:
    return bool(re.search(r"\d", quote or "")) or bool(set(leadnotes.normalize(quote).split()) & leadnotes.NUMBER_WORDS)


def refuses(quote: str) -> bool:
    q = " %s " % leadnotes.normalize(quote)
    return any((" %s " % leadnotes.normalize(r)) in q for r in REFUSALS)


def objection_of(v) -> str:
    s = leadnotes.normalize(str(v or ""))
    if "third" in s or "party" in s or any(w in s.split() for w in ("spouse", "wife", "husband", "family", "heir", "lawyer")):
        return "third party"
    if "trust" in s:
        return "trust"
    if "price" in s or "money" in s or "offer" in s:
        return "price"
    if "time" in s or "timing" in s or "when" in s.split():
        return "time"
    return "other"


def _true(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes")


def validate(raw: dict, transcript: str, checks, min_words: int = 3) -> tuple[dict, dict]:
    """(the flags that hold, why each other finding was dropped). The hard rule: no quote found in the
    right speaker's words, no flag."""
    parts = speakers(transcript)
    keep, dropped = {}, {}
    for k in checks:
        item = (raw or {}).get(k)
        if not isinstance(item, dict) or not _true(item.get("found")):
            continue
        quote = leadnotes.tidy(item.get("quote"), 300)
        if not quote:
            dropped[k] = "no quote"
            continue
        role = "agent" if k == "price" else "seller"
        if not quoted(quote, parts[role], min_words):
            dropped[k] = ("the quote is the other speaker's" if quoted(quote, parts["all"], min_words)
                          else "the quote is not in the call")
            continue
        if k == "price" and not has_number(quote):
            dropped[k] = "a price quote with no number in it"
            continue
        if k == "no_refusal" and refuses(quote):
            dropped[k] = "the quote is a refusal"
            continue
        f = {"quote": quote}
        if k == "missed_objection":
            f["objection"] = objection_of(item.get("objection"))
        keep[k] = f
    return keep, dropped


def parse_reply(text: str) -> dict | None:
    """The findings object out of the model's reply, or None. A reply with no findings in it is a call
    to try again, never a call with nothing wrong."""
    from call_coaching import grade
    for b in grade.json_blocks(text or ""):
        if any(k in b for k in CHECKS):
            return b
    return None


def scrub(text: str, names=()) -> str:
    """A quote as the channel may show it: a phone number and the owner's names taken out."""
    t = _SEVEN_RX.sub("[number]", _PHONE_RX.sub("[number]", str(text or "")))
    for n in names:
        n = str(n or "").strip()
        if len(n) >= 3:
            t = re.sub(r"\b%s\b" % re.escape(n), "[name]", t, flags=re.I)
    return leadnotes.tidy(t)


# ---------------------------------------------------------------------- the paid pieces, replaceable
def _engine():
    from call_coaching import grade, transcribe
    return grade, transcribe


def _fetch(url: str, max_bytes: int) -> dict:
    if FETCH is not None:
        return FETCH(url, max_bytes)
    return _engine()[1].fetch_recording(url, max_bytes=max_bytes)


def _transcribe(audio: bytes, fmt: str, caller: str) -> tuple[str, dict]:
    if TRANSCRIBE is not None:
        return TRANSCRIBE(audio, fmt, caller)
    return _engine()[1].transcribe_bytes(audio, fmt, caller or None)


def _billed(usage) -> float:
    return _engine()[1].billed(usage if isinstance(usage, dict) else {})


def _usd(usage, model: str) -> float:
    return _engine()[0].usage_usd(usage, model)


def _ask(system: str, user: str, model: str, max_tokens: int) -> tuple[str, dict, str]:
    """One read of one call by Claude, the way voice._ask_claude does it: thinking off (Haiku 5.5 takes
    that at effort high or below) and effort low, since finding a sentence in a transcript needs no
    reasoning. A model that refuses "disabled" (Opus 5.5, Sonnet 5.5) is asked again without it."""
    if ASK is not None:
        return ASK(system, user, model, max_tokens)
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    base = dict(model=model, system=system, output_config={"effort": "low"},
                messages=[{"role": "user", "content": user}])
    try:
        msg = client.messages.create(max_tokens=max_tokens, thinking={"type": "disabled"}, **base)
    except anthropic.BadRequestError:
        msg = client.messages.create(max_tokens=max_tokens + 4000, **base)
    u = getattr(msg, "usage", None)
    usage = {k: int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                      "cache_creation_input_tokens")}
    text = "".join(getattr(b, "text", "") or "" for b in (msg.content or []) if getattr(b, "type", "") == "text")
    return text, usage, str(getattr(msg, "stop_reason", "") or "")


def keys_missing() -> dict:
    """{"screen": key or "", "audio": key or ""}: the keys a live run needs that the box does not have.
    Screening needs Claude; only a call nobody transcribed yet needs OpenRouter."""
    out = {"screen": "", "audio": ""}
    if ASK is None and not str(config.ANTHROPIC_API_KEY or "").strip():
        out["screen"] = "ANTHROPIC_API_KEY"
    if TRANSCRIBE is None:
        try:
            _engine()[1]._env("OPENROUTER_API_KEY")
        except KeyError:
            out["audio"] = "OPENROUTER_API_KEY"
    return out


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _clock(dt: datetime, tz: str) -> str:
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %s %d, %s" % (d.strftime("%a"), d.strftime("%b"), d.day, d.strftime("%I:%M %p").lstrip("0"))


def _day(dt: datetime, tz: str) -> str:
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %d" % (d.strftime("%b"), d.day)


def _status_name(s: str) -> str:
    low = str(s or "").strip().lower()
    return {"not_interested": "Not Interested", "dead lead": "Dead Lead", "lost_deal": "Lost Deal",
            "under_contract": "Under Contract"}.get(low, str(s or "").strip() or "a new status")


# ---------------------------------------------------------------------- the flags table
def ensure() -> None:
    kpi.ensure("ap_callscreen", SCHEMA)


def save_flag(stage: str, live: bool, item: dict, kind: str, f: dict, now: datetime, tz: str, task: str = "") -> None:
    ensure()
    with store._LOCK, store._db() as c:
        c.execute("INSERT INTO ap_callscreen (at, day, live, stage, call_id, owner, record, street, caller, call_at, kind, "
                  "detail, quote, task) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (now.isoformat(timespec="seconds"), ledger.day(now, tz), 1 if live else 0, stage, item["call"],
                   item["owner"], item.get("record") or "", item.get("street") or "", item.get("caller") or "",
                   item["when"].isoformat(timespec="seconds"), kind, f.get("objection") or f.get("detail") or "",
                   f.get("shown") or f.get("quote") or "", task or ""))


def flags(since_day: str = "", live: bool | None = None, kind: str = "") -> list[dict]:
    """The flags the screen has raised, oldest first: for the coaching picks and the operations
    questions that follow a flag ("which calls gave a price this week")."""
    ensure()
    q, args = "SELECT * FROM ap_callscreen WHERE 1=1", []
    if since_day:
        q += " AND day>=?"
        args.append(since_day)
    if live is not None:
        q += " AND live=?"
        args.append(1 if live else 0)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    with store._LOCK, store._db() as c:
        return [dict(r) for r in c.execute(q + " ORDER BY id", args).fetchall()]


# ---------------------------------------------------------------------- one run
class _Run:
    def __init__(self, ctx):
        self.ctx = ctx
        self.st = settings(ctx.stage)
        self.live = bool(ctx.live)
        self.tz = ctx.policy.tz
        self.leads = _lows(self.st["lead_statuses"])
        self.reads = self.transcribed = self.asked = 0
        self.rows: dict[str, list | None] = {}
        self.records: dict[str, dict | None] = {}
        self.items: dict[str, dict] = {}
        self.order: list[str] = []
        self.raised: list[dict] = []       # (item, kind, flag) of this run, live
        self.screened = 0
        self.on_hand = self.to_buy = 0
        self.no_after: list[str] = []       # streets whose call was followed by Not Interested or Dead Lead
        self.quiet: list[str] = []          # streets of the quiet or fallen leads read
        self.waiting: Counter = Counter()
        self.unopened: Counter = Counter()
        self.task_state: dict = {}
        self.names = None
        self.missing = {"screen": "", "audio": ""}
        self.start = self.end = ctx.now

    # ------------------------------------------------------------------ marks and reads
    def _kv(self, what: str) -> str:
        return "autopilot:callscreen:%s:%s:%s" % (self.ctx.stage.id, what, "live" if self.live else "plan")

    def mark(self, subject: str) -> dict:
        v = ledger.fact(subject, KEY if self.live else PLAN_KEY)
        return v if isinstance(v, dict) else {}

    def set_mark(self, subject: str, status: str, **detail) -> None:
        ledger.set_fact(subject, KEY if self.live else PLAN_KEY,
                        dict(detail, status=status, at=self.ctx.now.isoformat(timespec="seconds")))

    def _read(self) -> bool:
        if self.reads >= int(self.st["max_reads"]):
            self.waiting["the run's read cap"] += 1
            return False
        self.reads += 1
        return True

    def owner_rows(self, owner: str) -> list | None:
        if owner in self.rows:
            return self.rows[owner]
        if not self._read():
            return None
        try:
            rows = list(self.ctx.crm.owner_records(owner, cap=10) or [])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not look up the houses of a called owner (%s)" % str(exc)[:80])
            rows = None
        self.rows[owner] = rows
        return rows

    def record(self, u: str) -> dict | None:
        if u in self.records:
            return self.records[u]
        if not u or not self._read():
            return None
        try:
            rec = self.ctx.crm.record(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read a called owner's record for the call screen (%s)" % str(exc)[:80])
            rec = None
        if rec is not None:
            rec.setdefault("uuid", u)
        self.records[u] = rec
        return rec

    # ------------------------------------------------------------------ the run
    def run(self) -> None:
        if self.st["sync"]:
            for g in ("calls", "state", "board"):
                try:
                    activity.sync(self.ctx.crm, g, now=self.ctx.now, max_calls=int(self.st["sync_calls"]))
                except budget.BudgetExhausted:
                    raise
                except Exception as exc:  # noqa: BLE001  (a stale copy still holds what it has)
                    self.ctx.out.problem("could not refresh the %s log before the call screen (%s)" % (g, str(exc)[:80]))
        if self.live:
            self.missing = keys_missing()
            if self.missing["screen"]:
                self.ctx.out.problem("The call screen needs %s on the brain, so no call was screened" % self.missing["screen"])
                self.report()
                return
        self.collect()
        self.quiet_leads()
        open_calls, open_quiet = [], []
        for cid in self.order:
            it = self.items[cid]
            state = self.one(it)
            if state == "open":
                if it["today"]:
                    open_calls.append(it["when"])
                for tid in it["triggers"]:
                    open_quiet.append(tid)
            elif state == "done":
                for tid in it["triggers"]:
                    self.trigger_done(tid, cid)
        store.kv_set(self._kv("calls"), (min(open_calls) if open_calls else self.end).isoformat(timespec="seconds"))
        self.close_triggers(set(open_quiet))
        self.report()

    def collect(self) -> None:
        """Every answered call of `min_seconds` or more since the last run, settled, not screened yet."""
        st, ctx = self.st, self.ctx
        self.end = ctx.now - timedelta(minutes=float(st["settle_minutes"]))
        raw = store.kv_get(self._kv("calls"))
        try:
            self.start = datetime.fromisoformat(raw) if raw else self.end - timedelta(hours=float(st["first_hours"]))
        except ValueError:
            self.start = self.end - timedelta(hours=float(st["first_hours"]))
        seen = set()
        floor = ctx.now - timedelta(days=float(st["max_age_days"]))
        for e in activity.events(tuple(st["call_events"]), since=self.start - activity.OVERLAP, until=self.end):
            d = e.get("data") or {}
            cid = str(d.get("call") or e.get("id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            if _int(d.get("duration")) < int(st["min_seconds"]):
                continue
            if self.mark("call:%s" % cid).get("status") in FINAL:
                continue
            if e["when"] < floor:
                # A call that waited this long (a cap, a missing key) is closed, so the window moves on.
                ctx.out.tally["closed: waited more than %d days" % int(float(st["max_age_days"]))] += 1
                self.set_mark("call:%s" % cid, "skipped", why="waited too long")
                continue
            self.add(cid, e, today=True)

    def add(self, cid: str, e: dict, today: bool = False) -> dict:
        d = e.get("data") or {}
        it = self.items.get(cid)
        if it is None:
            it = self.items[cid] = {"call": cid, "owner": str(e.get("resource") or ""), "when": e["when"],
                                    "seconds": _int(d.get("duration")), "recording": str(d.get("recording") or ""),
                                    "caller": str(d.get("caller") or "").strip(), "today": False, "checks": [],
                                    "context": [], "triggers": []}
            self.order.append(cid)
        if today and not it["today"]:
            it["today"] = True
            it["checks"] = ["price", "soft_yes"] + [c for c in it["checks"] if c not in ("price", "soft_yes")]
        return it

    def quiet_leads(self) -> None:
        """Every lead that went quiet, or whose contract fell apart, since the last run: its last calls
        are read for the objection nobody uncovered."""
        st, ctx = self.st, self.ctx
        raw = store.kv_get(self._kv("quiet"))
        try:
            since = datetime.fromisoformat(raw) if raw else ctx.now - timedelta(hours=float(st["first_hours"]))
        except ValueError:
            since = ctx.now - timedelta(hours=float(st["first_hours"]))
        quiet, lost = _lows(st["quiet_statuses"]), _lows(st["lost_statuses"])
        contract, closed = _lows(st["contract_statuses"]), _lows(st["closed_statuses"])
        self.trigger_events = {}
        if int(st["quiet_calls"]) < 1:
            return
        for e in activity.events("property.status.updated", since=since - activity.OVERLAP, until=ctx.now):
            d = e.get("data") or {}
            to, frm = str(d.get("to") or "").strip().lower(), str(d.get("from") or "").strip().lower()
            if to in quiet:
                why = "this lead went quiet (moved to %s on %s)" % (str(d.get("to")).strip(), _day(e["when"], self.tz))
            elif to in lost or (frm in contract and to not in contract and to not in closed):
                why = "its contract fell apart (moved from %s to %s on %s)" % (
                    _status_name(d.get("from")), _status_name(d.get("to")), _day(e["when"], self.tz))
            else:
                continue
            tid = "quiet:%s" % e.get("id")
            m = self.mark(tid)
            if m.get("status") in FINAL:
                continue
            u = str(e.get("resource") or "")
            rec = self.record(u)
            if rec is None:
                self.trigger_events[tid] = {"when": e["when"], "open": True}
                continue
            owner = (rec.get("owner") or {}).get("uuid") or ""
            if not owner or _is_entity(rec.get("owner") or {}):
                self.set_mark(tid, "skipped", why="a company or trust owns it (people only)" if owner else "no owner")
                continue
            self.trigger_events[tid] = {"when": e["when"], "open": False, "done": list(m.get("calls") or [])}
            back = e["when"] - timedelta(days=float(st["quiet_days"]))
            calls, seen = [], set()
            for c in activity.events(tuple(st["call_events"]), resource=owner, since=back, until=e["when"]):
                cd = c.get("data") or {}
                cid = str(cd.get("call") or c.get("id") or "")
                if cid and cid not in seen and _int(cd.get("duration")) >= int(st["min_seconds"]):
                    seen.add(cid)
                    calls.append((cid, c))
            todo = [(cid, c) for cid, c in calls[-int(st["quiet_calls"]):] if cid not in self.trigger_events[tid]["done"]]
            if not todo:
                self.ctx.out.tally["quiet or fallen leads with no call of %d s or more to read" % int(st["min_seconds"])] += 1
                self.set_mark(tid, "screened", calls=self.trigger_events[tid]["done"])
                continue
            self.quiet.append(addr(rec))
            for cid, c in todo:
                it = self.add(cid, c)
                if "missed_objection" not in it["checks"]:
                    it["checks"].append("missed_objection")
                it["context"].append(why)
                it["triggers"].append(tid)
                it.setdefault("quiet_record", rec)

    def trigger_done(self, tid: str, cid: str) -> None:
        t = self.trigger_events.get(tid)
        if t is not None and cid not in t["done"]:
            t["done"].append(cid)

    def close_triggers(self, still_open: set) -> None:
        """A quiet lead is finished once each of its calls is read; one held by a cap waits whole."""
        opened = []
        for tid, t in getattr(self, "trigger_events", {}).items():
            if t.get("open") or tid in still_open:
                opened.append(t["when"])
                if not t.get("open"):
                    self.set_mark(tid, "open", calls=t.get("done") or [])
                continue
            if self.mark(tid).get("status") not in FINAL:
                self.set_mark(tid, "screened", calls=t.get("done") or [])
        store.kv_set(self._kv("quiet"), (min(opened) if opened else self.ctx.now).isoformat(timespec="seconds"))

    # ------------------------------------------------------------------ one call
    def house(self, it: dict):
        """(the owner's houses, the one house the call is about or None, the owner) or None when this run
        cannot tell (the read cap). A house is named when the owner has one, or exactly one is a lead."""
        rows = self.owner_rows(it["owner"])
        if rows is None:
            return None
        lead = [r for r in rows if lanes.status_of(r) in self.leads]
        pick = rows[0] if len(rows) == 1 else (lead[0] if len(lead) == 1 else None)
        owner = next((r.get("owner") for r in rows if (r.get("owner") or {}).get("first_name")
                      or (r.get("owner") or {}).get("last_name") or (r.get("owner") or {}).get("company")), None)
        if owner is None and rows:
            rec = self.record(str(rows[0].get("uuid") or ""))
            if rec is None:
                return None
            owner = rec.get("owner") or {}
        return rows, pick, owner or {}

    def one(self, it: dict) -> str:
        """One call: "done" (screened or skipped for good), or "open" (try again next run)."""
        st, ctx = self.st, self.ctx
        subject = "call:%s" % it["call"]
        got = self.house(it)
        if got is None:
            return "open"
        rows, pick, owner = got
        if not rows:
            return self.skip(subject, "no house on file for the owner")
        if _is_entity(owner):
            return self.skip(subject, "a company or trust owns it (people only)")
        quiet_rec = it.get("quiet_record")
        house = quiet_rec or pick
        it["record"] = str((house or {}).get("uuid") or "")
        it["street"] = addr(house) if house else "%s's %d houses" % ((owner.get("first_name") or "the owner"), len(rows))
        it["names"] = [owner.get("first_name"), owner.get("last_name")]
        if it["today"]:
            after = self.decided_after(rows, it["when"])
            if after:
                if "no_refusal" not in it["checks"]:
                    it["checks"].append("no_refusal")
                it["context"].append(after)
                self.no_after.append(it["street"])
        text = transcript_of(it["call"])
        est_t = 0.0
        if not text:
            why = self.cannot_buy(it)
            if why:
                return self.skip(subject, why)
            if self.live and self.missing["audio"]:
                # Waits rather than skipped: once the key is on the brain these calls are screened.
                ctx.out.problem("The call screen needs %s on the brain to transcribe a call nobody transcribed yet; "
                                "those calls wait" % self.missing["audio"])
                self.waiting["no transcription key on the brain"] += 1
                return "open"
            est_t = round(max(1.0, it["seconds"] / 60.0) * float(st["audio_per_min"]), 4)
            if self.transcribed >= int(st["max_transcriptions"]):
                self.waiting["the run's transcription cap"] += 1
                return "open"
        if self.asked >= int(st["max_calls"]):
            self.waiting["the run's screening cap"] += 1
            return "open"
        est_s = float(st["screen_usd"])
        ok, why = ctx.budget(est_t + est_s)
        if not ok:
            self.waiting[why] += 1
            return "open"
        ctx.reserve(est_t + est_s)
        if est_t:
            self.to_buy += 1
        else:
            self.on_hand += 1
        if not self.live:
            self.transcribed += 1 if est_t else 0
            self.asked += 1
            self.screened += 1
            return self.done(subject, it, [])
        if not text:
            text, state = self.buy(it, subject, est_t)
            if not text:
                ctx.settle(est_s, 0, "call screen")
                return state
        checks = [c for c in CHECKS if c in it["checks"]]
        if call_kind(text) != "talk":
            checks = [c for c in checks if c == "price"]       # no seller on the line: only what our side said
            if not checks:
                ctx.settle(est_s, 0, "call screen")
                return self.skip(subject, "no seller on the recording (a voicemail or a greeting)")
        raw, why = self.ask(text, checks, it, est_s)
        if raw is None:
            return self.bump(subject, why)
        keep, dropped = validate(raw, text, checks, int(st["min_quote_words"]))
        for k, w in dropped.items():
            ctx.out.tally["findings dropped (%s): %s" % (LABELS[k], w)] += 1
        if "soft_yes" in keep:
            followed = self.followed_up(rows, it["when"])
            if followed:
                ctx.out.tally["soft yeses that were followed up (%s)" % followed] += 1
                keep.pop("soft_yes")
        self.screened += 1
        for k in CHECKS:
            if k in keep:
                f = keep[k]
                f["shown"] = scrub(f["quote"], it["names"])
                task = self.follow_up(it, rows, pick, f) if k == "soft_yes" else ""
                save_flag(ctx.stage.id, True, it, k, f, ctx.now, self.tz, task)
                self.raised.append({"item": it, "kind": k, "flag": f, "task": task})
        return self.done(subject, it, sorted(keep))

    def done(self, subject: str, it: dict, kinds: list[str]) -> str:
        self.set_mark(subject, "screened", flags=kinds, owner=it["owner"], record=it.get("record") or "")
        return "done"

    def skip(self, subject: str, why: str) -> str:
        self.ctx.out.tally["skipped: %s" % why] += 1
        self.set_mark(subject, "skipped", why=why)
        return "done"

    def bump(self, subject: str, why: str) -> str:
        tries = _int(self.mark(subject).get("tries")) + 1
        if tries >= int(self.st["max_tries"]):
            self.ctx.out.tally["closed: %s" % why] += 1
            self.set_mark(subject, "skipped", why=why, tries=tries)
            return "done"
        self.set_mark(subject, "open", why=why, tries=tries)
        self.waiting["a try again (%s)" % why] += 1
        return "open"

    def cannot_buy(self, it: dict) -> str:
        """Why a missing transcript cannot be bought, or "" when it can."""
        st = self.st
        if not st["transcribe"]:
            return "no transcript on hand, and buying one is off"
        if not it["recording"]:
            return "no recording in the call log"
        if it["seconds"] > float(st["max_minutes"]) * 60:
            return "longer than %d minutes" % int(float(st["max_minutes"]))
        return ""

    def decided_after(self, rows: list[dict], when: datetime) -> str:
        """Not Interested or Dead Lead set on one of the owner's houses soon after the call, in words
        for the model, or "". The code reads it off the state log; the model is never asked to guess it."""
        no = _lows(self.st["no_statuses"])
        until = when + timedelta(hours=float(self.st["disposition_hours"]))
        for r in rows:
            for e in activity.events("property.status.updated", resource=str(r.get("uuid") or ""), since=when, until=until):
                to = str((e.get("data") or {}).get("to") or "")
                if to.strip().lower() in no:
                    return "the record was marked %s %s after the call" % (
                        _status_name(to), routes.span((e["when"] - when).total_seconds()))
        return ""

    def followed_up(self, rows: list[dict], when: datetime) -> str:
        """What followed a soft yes on any of the owner's houses, or "" when nothing did."""
        for r in rows:
            u = str(r.get("uuid") or "")
            if lanes.status_of(r) in self.leads:
                return "the owner is a lead"
            if _int((r.get("tasks") or {}).get("incomplete")) > 0:
                return "an open task"
            if activity.events(("task.created",), resource=u, since=when):
                return "a task made after the call"
            if any(str((e.get("data") or {}).get("to") or "").strip().lower() in self.leads
                   for e in activity.events("property.status.updated", resource=u, since=when)):
                return "made a lead after the call"
        return ""

    def buy(self, it: dict, subject: str, est_t: float) -> tuple[str, str]:
        """The recording, opened from the log's own link and transcribed, then kept in lead notes' store
        so no stage pays for this call again. ("", state) when there is no transcript this run."""
        st, ctx = self.st, self.ctx
        got = _fetch(it["recording"], int(float(st["max_mb"]) * 1_000_000)) or {}
        if not got.get("ok"):
            ctx.settle(est_t, 0, "transcription")
            code = str(got.get("code") or "error")
            code = code if code in leadnotes.UNOPENED else "error"
            if code == "gone" and ctx.now - it["when"] < timedelta(hours=float(st["recording_grace_hours"])):
                self.waiting["a recording the dialer has not finished"] += 1
                return "", "open"
            if code == "error":
                return "", self.bump(subject, "the recording did not open")
            self.unopened[code] += 1
            return "", self.skip(subject, leadnotes.UNOPENED[code])
        try:
            text, usage = _transcribe(got.get("audio") or b"", str(got.get("fmt") or "mp3"), it["caller"])
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (OpenRouter down: this call waits, the run goes on)
            ctx.settle(est_t, 0, "transcription")
            return "", self.bump(subject, "the transcription failed (%s)" % str(exc)[:60])
        cost = _billed(usage) or est_t
        ctx.settle(est_t, cost, "transcribed a call")
        ledger.event(ctx.run_id, ctx.stage.id, it.get("record") or "", KIND, {"call": it["call"], "step": "transcribe"},
                     usd=cost, now=ctx.now, tz=self.tz)
        self.transcribed += 1
        text = leadnotes.tidy_transcript(text)
        if not text:
            return "", self.skip(subject, "the transcription came back empty")
        try:
            f = leadnotes.transcript_file(it["call"])
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("call screen: could not keep a transcript (%s); a later stage would pay for it again", exc)
        return text, "ok"

    def ask(self, text: str, checks: list[str], it: dict, est_s: float) -> tuple[dict | None, str]:
        st, ctx = self.st, self.ctx
        model = str(st["model"])
        try:
            reply, usage, stop = _ask(SYSTEM, user_text(text, checks, it["context"]), model, int(st["max_tokens"]))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the model down: the transcript is kept, the call waits)
            ctx.settle(est_s, 0, "call screen")
            return None, "the screening model did not answer (%s)" % str(exc)[:60]
        cost = _usd(usage, model) or est_s
        ctx.settle(est_s, cost, "screened a call")
        ledger.event(ctx.run_id, ctx.stage.id, it.get("record") or "", KIND, {"call": it["call"], "step": "screen"},
                     usd=cost, now=ctx.now, tz=self.tz)
        self.asked += 1
        if str(stop or "") in ("refusal", "max_tokens"):
            return None, "the screen came back cut off or refused"
        raw = parse_reply(reply)
        if raw is None:
            return None, "the screen came back without the findings"
        return raw, ""

    # ------------------------------------------------------------------ the follow-up task (live only)
    def follow_up(self, it: dict, rows: list[dict], pick: dict | None, f: dict) -> str:
        """One task for the record's assignee on a soft yes nothing followed. Returns the assignee's
        name when a task was made, "" otherwise (the reason is counted). Never a text, never a call."""
        st, ctx = self.st, self.ctx
        if not st["follow_up_task"] or self.task_state.get("broken"):
            return ""
        if pick is None:
            ctx.out.tally["soft yeses with no task: the owner has several houses and the call does not say which"] += 1
            return ""
        u = str(pick.get("uuid") or "")
        if any(e["detail"].get("call") == it["call"] for e in ledger.events(u, TASK_ACTION)):
            return ""
        rec = self.record(u)
        if rec is None:
            ctx.out.tally["soft yeses with no task: the record could not be read"] += 1
            return ""
        if not ctx.passes(rec, "task"):
            return ""
        who = assignee_of(rec)
        if not who:
            ctx.out.problem("A soft yes on %s has no one assigned to the record, so no follow-up task was set" % addr(rec))
            return ""
        tz = self.tz
        local = ctx.now.astimezone(ZoneInfo(tz))
        ahead = max(1, (routes.business_day(ctx.now, tz, int(st["task_due_days"])).date() - local.date()).days)
        title = "Call back: the seller sounded open on the %s call" % _day(it["when"], tz)
        notes = ('The evening call screen read the call on %s: the seller said "%s", and no task or lead status followed. '
                 'One call to see where they stand.' % (_clock(it["when"], tz), scrub(f["quote"])))
        ctx.change(u, "task_create", None, {"title": title})
        r = ctx.write("follow-up task", routes.create_task, ctx.crm, u, title, routes.end_of_day(ctx.now, tz, ahead), who,
                      notes)
        tid = routes.task_id(r)
        if not self.task_state.get("verified"):
            self.task_state["verified"] = True
            ok = False
            if tid:
                try:
                    back = routes.get_task(ctx.crm, tid)
                    ok = str(back.get("assigned_to_property") or u) == u and not back.get("completed")
                except budget.BudgetExhausted:
                    raise
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                self.task_state["broken"] = True
                ctx.out.problem("the task route answered and no follow-up task could be read back on %s; no more were "
                                "created. Create one task by hand and compare" % addr(rec))
                return ""
        ledger.event(ctx.run_id, ctx.stage.id, u, TASK_ACTION, {"task": tid, "call": it["call"], "why": "soft yes"},
                     now=ctx.now, tz=tz)
        ctx.out.tally["follow-up tasks set"] += 1
        return self.name(who)

    def name(self, user_uuid: str) -> str:
        if self.names is None:
            self.names = routes.names_from_log(since=self.ctx.now - timedelta(days=60))
        return str(self.names.get(user_uuid) or "the assignee")

    # ------------------------------------------------------------------ what it says
    def line(self, r: dict) -> str:
        it, k, f = r["item"], r["kind"], r["flag"]
        who = "%s, %s" % (_clock(it["when"], self.tz), it["caller"]) if it["caller"] else _clock(it["when"], self.tz)
        q = '"%s"' % f["shown"]
        if k == "price":
            text = "%s (%s): a price on the phone: %s" % (it["street"], who, q)
        elif k == "soft_yes":
            text = "%s (%s): the seller sounded open and nothing followed: %s" % (it["street"], who, q)
            if r.get("task"):
                text += ". A follow-up task is set for %s" % r["task"]
        elif k == "no_refusal":
            after = next((c for c in it["context"] if c.startswith("the record was marked")), "it was marked")
            text = "%s (%s): %s, but the seller said %s" % (it["street"], who, after.replace("the record was ", ""), q)
        else:
            text = "%s (%s): %s nobody asked about: %s" % (it["street"], who, OBJECTIONS[f.get("objection") or "other"], q)
        # Only the dashes are cleaned here: tidy() also strips quote marks off the ends, which would cut
        # the closing mark off a line that ends in its quote.
        return " ".join(text.replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-").split())

    def report(self) -> None:
        ctx, out, st = self.ctx, self.ctx.out, self.st
        out.found = len(self.items)
        n_flags = len(self.raised)
        out.acted = n_flags if self.live else self.screened
        wait = sum(self.waiting.values())
        since = _clock(self.start, self.tz)
        if self.live:
            if self.screened or n_flags:
                kinds = Counter(r["kind"] for r in self.raised)
                if n_flags:
                    body = "%d flagged (%s)" % (n_flags, ", ".join("%d %s" % (kinds[k], LABELS[k]) for k in CHECKS if kinds[k]))
                else:
                    body = "nothing flagged"
                out.head = "Screened %s since %s: %s." % (_plural(self.screened, "call"), since, body)
            if wait:
                out.head = (out.head + " " if out.head else "") + "%s for the next run." % (
                    "1 call waits" if wait == 1 else "%d calls wait" % wait)
            for r in self.raised[:int(st["max_flag_lines"])]:
                out.line(self.line(r))
            if n_flags > int(st["max_flag_lines"]):
                out.line("And %d more flagged calls in the call screen's table." % (n_flags - int(st["max_flag_lines"])))
        else:
            if self.screened:
                out.head = "Would screen %s since %s: %d %s on hand, %d to transcribe, about $%.2f." % (
                    _plural(self.screened, "call"), since, self.on_hand, "transcript" if self.on_hand == 1 else "transcripts",
                    self.to_buy, max(out.would_usd, 0.01))
            if wait:
                out.head = (out.head + " " if out.head else "") + "%s for the next run." % (
                    "1 call waits" if wait == 1 else "%d calls wait" % wait)
            if self.no_after:
                out.line("%s followed by Not Interested or Dead Lead, each to be checked for a real refusal (%s)" % (
                    _plural(len(self.no_after), "call"), "; ".join(self.no_after[:3])))
        if self.quiet:
            out.line("%s went quiet or fell apart since the last run: %s %s for a missed objection (%s)" % (
                _plural(len(self.quiet), "lead"), "their last calls" if len(self.quiet) > 1 else "its last calls",
                "were read" if self.live else "would be read", "; ".join(self.quiet[:3])))
        for why, k in self.waiting.most_common(2):
            out.line("Waiting: %s, held by %s." % (_plural(k, "call"), why))
        if self.unopened:
            out.problem("Some call recordings could not be opened, so those calls were not screened: %s" % "; ".join(
                "%s: %d" % (leadnotes.UNOPENED_SHORT[c], k) for c, k in self.unopened.most_common()))


def run_callscreen(ctx) -> None:
    _Run(ctx).run()
