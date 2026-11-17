"""The attempt log on every lead: one pinned running note, one dated line per call (Ty, 2026-11-15).

Why it exists. Measured over 30 days of prod-account's own log (2026-10-16 to 11-15): the lead manager typed
165 notes that said nothing but "no answer", "left vm" or "no answer no vm", about 13 a business day,
a median 12 to 30 seconds after the dial, and kept one running note per lead by pinning it (45 pins)
and adding to it (29 of her 55 note edits appended a line). Every one of those facts is already in
the call log the brain keeps a copy of (activity.py): the dial, whether it was answered, for how
long, and the recording. No stage wrote them down, so a person did, by hand, after every dial.

What a run does:
  1. reads the outbound calls in the log copy since its last run. The log writes one event per call
     STATE (made, answered, noanswer), so a call is folded on its call id; a dial that never got an
     answer row is a call that was never answered;
  2. keeps a call only when one of the owner's houses held a LEAD status at the moment of the dial.
     The status is read back through the state log from what the record holds now, because the
     dialer sets Not Interested seconds after the call that ended a lead, and that call belongs on
     the log. A dial made just before the record became a lead (lead_grace_minutes) is the call that
     made it one, and it is attempt 1;
  3. writes ONE line per call: the date and time, the attempt number (calls to this owner since it
     became a lead) and the outcome;
  4. keeps ONE running note per lead on the owner's message board (the board is per owner: the
     documented route is /api/internal/owner/{owner_uuid}/message/), and edits that note instead of
     adding a new one each time.

Rules, each from what goes wrong without it:
  * THE OUTCOME IS WHAT THE LOG AND THE RECORDING SAY, NEVER A GUESS FROM THE LENGTH. A voicemail
    greeting answers a call too, and several answered calls of a minute or more were noted by hand
    as "left vm", so a duration cannot tell a voicemail from a conversation. With a transcript (the
    one lead notes or call coaching already paid for), a call with a SELLER line is a conversation
    ("talked 4 min", and "see the lead note" when lead notes wrote one for that call) and a call
    without one is a voicemail. With no transcript the line says only "answered, 45 s". This stage
    pays for nothing: an answered call long enough for lead notes to transcribe waits for that
    transcript (transcript_wait_minutes), then says what it can.
  * NEVER TWO PINNED NOTES ON ONE LEAD. A new log is pinned only when nothing else on the board is
    pinned; under a person's own pinned note it goes in unpinned and is pinned the day that note is
    gone. When a log is full and continues in a new note, the old one is unpinned first.
  * A PERSON'S WORDS ARE NEVER OVERWRITTEN. A new line is added to the note's CURRENT text, as read
    back from the board, so a line a person typed into the log stays. A log a person unpinned or
    deleted is left as they left it, and the next call starts a new one.
  * LINES STAY IN ORDER. A call that is not decidable yet (its answer row may still land, its
    transcript is still being written) holds every later call to the same owner, so attempt 4 never
    appears above attempt 3.
  * THE FIRST LIVE WRITE OF A RUN IS READ BACK. The owner board's create route was unproven on the
    Open API key when this was written; a note that does not read back stops every other note that
    run instead of leaving one unreadable note per lead.
  * People only: a house a company or trust owns is never logged. The gate runs on every lead record
    (channel research: a note contacts nobody, so only Do Not Market, a litigator, an opt-out, a sale
    or a finished status hold it; the playbook lifts not_interested, for the call that ended the lead).

Plan mode reads exactly what live mode reads (the log copy, the owner's records, the board when a
new log would be started) and says the lines it would write; it writes nothing to the account. Its
marks are its own, so a later live run still writes every line a plan run only counted.

Privacy: a line names the outcome and the caller's first name, never a phone number and never words
from the call. Every live write records the text it replaced (undo_step puts it back; a created log
is deleted, which the board's own documented route allows).
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import store
from . import activity, budget, lanes, ledger, leadnotes
from .crm import rows_of
from .stages import addr

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

KIND = "attemptlog"
WHAT = "attempt_note"                 # the change kind and the ledger action of every board write
KEY = "attemptlog"                    # ledger fact: on an owner, the log note; on "call:<id>", the call's mark
PLAN_KEY = "attemptlog_plan"
CALL_TYPES = (activity.DIAL, "owner.call.answered", "owner.call.noanswer")
MARKER = "Attempt log"
HEADER = "Attempt log: one line per call to this owner while a lead, written from the call log."
CONTINUED = "Attempt log, continued from the previous note: one line per call to this owner while a lead."
DONE = ("written", "skipped")
DISPOSITIONS = {"WRONG": "number marked wrong", "WRONG_DNC": "number marked wrong", "DEAD": "number marked dead",
                "DNC": "number marked do not call", "CORRECT_DNC": "number marked do not call",
                "CORRECT": "number marked correct"}
VOICE_OUT = {"voicemail_left": "voicemail, message left", "voicemail": "voicemail, no message left",
             "nobody": "answered, no conversation on the recording"}
DEFAULTS = {
    "statuses": list(lanes.LEAD_STATUS_VALUES),   # the statuses that make an owner a lead
    "settle_minutes": 10,           # a dial this fresh may still get its answer row
    "first_hours": 24,              # how far back the first run reaches
    "lead_grace_minutes": 30,       # a dial this soon before the record became a lead made it one
    "transcript_seconds": 90,       # an answered call this long waits for lead notes' transcript
    "transcript_wait_minutes": 120, # the longest it waits before saying "answered" and the length
    "disposition_minutes": 30,      # a phone status set this soon after the dial is the call's disposition
    "pair_minutes": 10,             # an answer row with no call id joins a dial to the same number this soon before it
    "max_owners": 20,               # owners whose log is written a run
    "max_reads": 80,                # DataSift reads a run (the owner's houses, the lead record, the board)
    "board_page": 50,               # board messages read when a new log is started
    "max_records": 2,               # lead records read per owner for the gate
    "note_max": 3900,               # a log longer than this continues in a new note
    "caller_names": True,           # name the caller on an answered call's line
    "sync": True,
    "sync_calls": 40,               # log pages read to bring the calls and state copy up to date first
}
EM, EN = chr(0x2014), chr(0x2013)

_TURN_RX = re.compile(r"^\s*(AGENT_VM|AGENT|SELLER|VOICEMAIL)\s*:\s?(.*)$", re.I)
_SUMMARY_RX = re.compile(r"^\s*DELIVERY SUMMARY\b", re.I)


# ---------------------------------------------------------------------- settings
def settings(st) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    for k in DEFAULTS:
        v = st.get(k) if hasattr(st, "get") else None
        if v is not None:
            out[k] = v
    return out


def block_problems(st) -> list[str]:
    """What is wrong with an attempt log block, for the playbook checker: a typo fails `autopilot
    check` instead of reading as a default on the first live morning."""
    out = []
    for key, low in (("max_owners", 1), ("max_reads", 1), ("board_page", 1), ("max_records", 1), ("note_max", 500),
                     ("sync_calls", 1)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    for key in ("settle_minutes", "first_hours", "lead_grace_minutes", "transcript_seconds", "transcript_wait_minutes",
                "disposition_minutes", "pair_minutes"):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            out.append("stage %s: %s must be a number, 0 or more" % (st.id, key))
    s = st.get("statuses")
    if s is not None and (not isinstance(s, list) or not s or [x for x in s if not str(x or "").strip()]):
        out.append("stage %s: statuses lists the lead statuses, as the account stores them" % st.id)
    for key in ("caller_names", "sync"):
        if st.get(key) is not None and not isinstance(st.get(key), bool):
            out.append("stage %s: %s is true or false" % (st.id, key))
    return out


# ---------------------------------------------------------------------- what a recording says (shared with the call screen)
def transcript_of(call_id: str) -> str:
    """The transcript of one call that some stage already paid for: lead notes keeps its own on the
    brain's volume, and the call coaching workflow (and the new-hire grading that writes into it)
    keeps one in wf_coach_grades. "" when neither holds one. Nothing is ever bought here."""
    if not call_id:
        return ""
    f = leadnotes.transcript_file(call_id)
    if f.is_file():
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        from ..workflows import call_coaching
        row = call_coaching.stored(call_id) or {}
    except Exception:  # noqa: BLE001  (no coaching table on this box yet: nothing to reuse)
        return ""
    return str(row.get("transcript") or "")


def turns(text: str) -> list[tuple[str, str]]:
    """(label, what was said) for every speaker turn, in order. A line with no label continues the turn
    above it. The transcriber ends with a DELIVERY SUMMARY about the call, which nobody said, so
    reading stops there: a quote from it is not a quote from the call."""
    out: list[list[str]] = []
    for line in str(text or "").splitlines():
        if _SUMMARY_RX.match(line):
            break
        m = _TURN_RX.match(line)
        if m:
            out.append([m.group(1).upper(), m.group(2)])
        elif out and line.strip():
            out[-1][1] += " " + line.strip()
    return [(a, b) for a, b in out]


def call_kind(text: str) -> str:
    """talk, voicemail_left, voicemail or nobody, from the speaker labels the transcriber writes
    (AGENT, SELLER, VOICEMAIL for a greeting, AGENT_VM for a message our caller leaves). A call with a
    SELLER line is a conversation; one without is not, however long it ran."""
    labels = {a for a, _ in turns(text)}
    if "SELLER" in labels:
        return "talk"
    if "AGENT_VM" in labels:
        return "voicemail_left"
    if "VOICEMAIL" in labels:
        return "voicemail"
    return "nobody"


# ---------------------------------------------------------------------- the calls, from the log copy
def _int(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def fold(events: list[dict], pair_minutes: float = 10) -> dict:
    """call key -> one outbound call: {key, call, owner, when, number, answered, noanswer}. The log
    writes one event per call state, all carrying the call's id; an answer row with no id joins the
    nearest earlier unanswered dial to the same number and owner, so one call is never counted as two."""
    out: dict[str, dict] = {}
    loose = []
    for e in sorted(events, key=lambda x: (x["when"], str(x.get("id")))):
        d = e.get("data") or {}
        if d.get("direction") == "inbound":
            continue
        cid = str(d.get("call") or "")
        if not cid and e.get("event_type") != activity.DIAL:
            loose.append(e)
            continue
        key = cid or ("ev:%s" % e.get("id"))
        c = out.get(key)
        if c is None:
            c = out[key] = {"key": key, "call": cid, "owner": str(e.get("resource") or ""), "when": e["when"],
                            "number": "", "answered": None, "noanswer": False}
        _merge(c, e)
    for e in loose:
        d = e.get("data") or {}
        near = [c for c in out.values() if c["owner"] == str(e.get("resource") or "") and c["answered"] is None
                and not c["noanswer"] and c["number"] == (d.get("number") or c["number"])
                and timedelta(0) <= e["when"] - c["when"] <= timedelta(minutes=float(pair_minutes))]
        if near:
            _merge(max(near, key=lambda c: c["when"]), e)
        else:
            key = "ev:%s" % e.get("id")
            out[key] = {"key": key, "call": "", "owner": str(e.get("resource") or ""), "when": e["when"], "number": "",
                        "answered": None, "noanswer": False}
            _merge(out[key], e)
    return out


def _merge(c: dict, e: dict) -> None:
    d = e.get("data") or {}
    if e["when"] < c["when"]:
        c["when"] = e["when"]
    if d.get("number") and not c["number"]:
        c["number"] = str(d["number"])
    et = e.get("event_type")
    if et == "owner.call.answered":
        c["answered"] = {"seconds": _int(d.get("duration")), "recording": str(d.get("recording") or ""),
                         "caller": str(d.get("caller") or "").strip()}
    elif et == "owner.call.noanswer":
        c["noanswer"] = True


def _status_at(record_uuid: str, current: str, when: datetime) -> str:
    """The record's status at a moment, read back from what it holds now through every change logged
    since. The dialer marks Not Interested seconds after the call that ended a lead, so the status
    a run sees is often not the status the call was made under."""
    s = str(current or "")
    for e in reversed(activity.events("property.status.updated", resource=record_uuid, since=when)):
        s = str((e.get("data") or {}).get("from") or "")
    return s.strip().lower()


def _entries(record_uuid: str, until: datetime, leads: set) -> list[datetime]:
    """When the record entered the lead statuses (from outside them), oldest first, up to a moment."""
    out = []
    for e in activity.events("property.status.updated", resource=record_uuid, until=until):
        d = e.get("data") or {}
        if str(d.get("to") or "").strip().lower() in leads and str(d.get("from") or "").strip().lower() not in leads:
            out.append(e["when"])
    return out


def _clock(dt: datetime, tz: str) -> str:
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %s %d, %s" % (d.strftime("%a"), d.strftime("%b"), d.day, d.strftime("%I:%M %p").lstrip("0"))


def _day(dt: datetime, tz: str) -> str:
    d = dt.astimezone(ZoneInfo(tz))
    return "%s %d" % (d.strftime("%b"), d.day)


def _dur(seconds) -> str:
    s = _int(seconds)
    return ("%d s" % s) if s < 120 else ("%d min" % round(s / 60.0))


def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _is_entity(owner: dict) -> bool:
    from deep_prospect_batch import is_entity
    return bool(is_entity(owner or {}))


def _code(exc) -> int:
    try:
        return int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _clean(text: str) -> str:
    return str(text or "").replace(" %s " % EM, ", ").replace(EM, ", ").replace(EN, "-")


# ---------------------------------------------------------------------- the owner's message board (documented routes)
def board_list(crm, owner_uuid: str, limit: int = 50) -> list[dict]:
    return rows_of(crm.call("/api/internal/owner/%s/message/?limit=%d&offset=0" % (owner_uuid, int(limit))))


def board_get(crm, owner_uuid: str, message_uuid: str) -> dict | None:
    """One message, or None when it is gone (a person deleted it). Any other error is raised."""
    try:
        r = crm.call("/api/internal/owner/%s/message/%s/" % (owner_uuid, message_uuid))
    except budget.BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        if _code(exc) == 404:
            return None
        raise
    return r if isinstance(r, dict) else None


def board_post(crm, owner_uuid: str, text: str) -> dict:
    return crm.call("/api/internal/owner/%s/message/" % owner_uuid, "POST", {"message": text}) or {}


def board_patch(crm, owner_uuid: str, message_uuid: str, text: str) -> dict:
    return crm.call("/api/internal/owner/%s/message/%s/" % (owner_uuid, message_uuid), "PATCH", {"message": text}) or {}


def board_pin(crm, owner_uuid: str, message_uuid: str) -> None:
    crm.call("/api/internal/owner/%s/message/%s/pin/" % (owner_uuid, message_uuid), "POST")


def board_unpin(crm, owner_uuid: str, message_uuid: str) -> None:
    crm.call("/api/internal/owner/%s/message/%s/unpin/" % (owner_uuid, message_uuid), "POST")


def board_delete(crm, owner_uuid: str, message_uuid: str) -> None:
    crm.call("/api/internal/owner/%s/message/%s/" % (owner_uuid, message_uuid), "DELETE")


def undo_step(crm, change: dict) -> str:
    """Put one recorded attempt log write back (undo.py routes the change kind attempt_note here). A
    log this run created is deleted; an edit gets the text it replaced; a pin or an unpin is turned
    around. An edit undone after a person added to the note puts back the text before the run, so
    their newer words go with it: undo is a deliberate act, and the change log says what it held."""
    before, after = change.get("before"), change.get("after") or {}
    owner = str((after or {}).get("owner") or (before or {}).get("owner") or "")
    if not owner:
        raise RuntimeError("the change does not name the owner whose board it was")
    if before is None:
        made = [e["detail"].get("message") for e in ledger.events(change.get("record_uuid") or "", WHAT)
                if e.get("run_id") == change.get("run_id") and e["detail"].get("made") and e["detail"].get("owner") == owner
                and e["detail"].get("message")]
        if not made:
            raise RuntimeError("the new log's id was not recorded, so it cannot be deleted from here")
        for m in made:
            board_delete(crm, owner, m)
        return "%d attempt log(s) deleted" % len(made)
    mid = str(before.get("message") or after.get("message") or "")
    if "text" in before:
        board_patch(crm, owner, mid, str(before.get("text") or ""))
        return "attempt log text put back"
    if "pinned" in before:
        (board_pin if before.get("pinned") else board_unpin)(crm, owner, mid)
        return "attempt log %s again" % ("pinned" if before.get("pinned") else "unpinned")
    raise RuntimeError("nothing recorded to put back")


# ---------------------------------------------------------------------- one run
class _Run:
    def __init__(self, ctx):
        self.ctx = ctx
        self.st = settings(ctx.stage)
        self.live = bool(ctx.live)
        self.tz = ctx.policy.tz
        self.leads = {str(s).strip().lower() for s in self.st["statuses"] if str(s).strip()}
        self.grace = timedelta(minutes=float(self.st["lead_grace_minutes"]))
        self.reads = 0
        self.owners = 0
        self.rows: dict[str, list | None] = {}
        self.records: dict[str, dict | None] = {}
        self.passed: dict[str, bool] = {}
        self.read_back = False          # the first live write of this run has been read back
        self.broken = False
        self.written: list[tuple[str, list[str], bool]] = []   # (street, line texts, a new log)
        self.unpinned: list[str] = []   # streets whose new log sits under a person's pinned note
        self.waiting: Counter = Counter()
        self.caught_up = activity.synced_through("calls") or ctx.now
        self.floor = None

    # ------------------------------------------------------------------ marks and reads
    def _kv(self) -> str:
        return "autopilot:attemptlog:%s:%s" % (self.ctx.stage.id, "live" if self.live else "plan")

    def mark(self, key: str) -> dict:
        v = ledger.fact("call:%s" % key, KEY if self.live else PLAN_KEY)
        return v if isinstance(v, dict) else {}

    def set_mark(self, key: str, status: str, **detail) -> None:
        ledger.set_fact("call:%s" % key, KEY if self.live else PLAN_KEY,
                        dict(detail, status=status, at=self.ctx.now.isoformat(timespec="seconds")))

    def skip(self, c: dict, why: str) -> None:
        if why:
            self.ctx.out.tally["skipped: %s" % why] += 1
        self.set_mark(c["key"], "skipped", why=why or "held back by the gate", owner=c["owner"])

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
            self.ctx.out.problem("could not look up the houses of a dialed owner (%s)" % str(exc)[:80])
            rows = None
        self.rows[owner] = rows
        return rows

    def record(self, u: str) -> dict | None:
        if u in self.records:
            return self.records[u]
        if not self._read():
            return None
        try:
            rec = self.ctx.crm.record(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read a lead record for its attempt log (%s)" % str(exc)[:80])
            rec = None
        if rec is not None:
            rec.setdefault("uuid", u)
        self.records[u] = rec
        return rec

    # ------------------------------------------------------------------ the run
    def run(self) -> None:
        if self.st["sync"]:
            for g in ("calls", "state"):
                try:
                    activity.sync(self.ctx.crm, g, now=self.ctx.now, max_calls=int(self.st["sync_calls"]))
                except budget.BudgetExhausted:
                    raise
                except Exception as exc:  # noqa: BLE001  (a stale copy still holds the calls it has)
                    self.ctx.out.problem("could not refresh the %s log before the attempt logs (%s)" % (g, str(exc)[:80]))
            self.caught_up = activity.synced_through("calls") or self.ctx.now
        calls = self.pending()
        self.ctx.out.found = len(calls)
        by_owner: dict[str, list] = {}
        for c in calls:
            by_owner.setdefault(c["owner"], []).append(c)
        open_at = []
        for owner, cs in by_owner.items():
            if self.broken:
                self.waiting["a board write that failed or did not read back"] += len(cs)
                open_at += [c["when"] for c in cs]
                continue
            if self.owners >= int(self.st["max_owners"]):
                self.waiting["the run's owner cap"] += len(cs)
                open_at += [c["when"] for c in cs]
                continue
            open_at += self.owner(owner, cs)
        store.kv_set(self._kv(), (min(open_at) if open_at else self.ctx.now).isoformat(timespec="seconds"))
        self.report()

    def pending(self) -> list[dict]:
        """Every outbound call since the last run that is not decided yet, oldest first."""
        raw = store.kv_get(self._kv())
        try:
            start = datetime.fromisoformat(raw) if raw else self.ctx.now - timedelta(hours=float(self.st["first_hours"]))
        except ValueError:
            start = self.ctx.now - timedelta(hours=float(self.st["first_hours"]))
        evs = activity.events(CALL_TYPES, since=start - activity.OVERLAP, until=self.ctx.now + timedelta(seconds=1))
        out = []
        for c in fold(evs, float(self.st["pair_minutes"])).values():
            if not c["owner"] or c["when"] < start - activity.OVERLAP:
                continue
            if self.mark(c["key"]).get("status") in DONE:
                continue
            out.append(c)
        out.sort(key=lambda c: (c["when"], c["key"]))
        return out

    def owner(self, owner: str, cs: list[dict]) -> list:
        """One owner's new calls: lines decided in order, then one write. Returns the times of the
        calls that wait for the next run."""
        rows = self.owner_rows(owner)
        if rows is None:
            return [c["when"] for c in cs]
        if not rows:
            for c in cs:
                self.skip(c, "no house on file for the owner")
            return []
        lines, held = [], []
        for i, c in enumerate(cs):
            found = self.leads_at(rows, c["when"])
            if not found:
                self.skip(c, "not a lead when dialed")
                continue
            leads, start = found
            out, why = self.outcome(c)
            if out is None:
                held = [x["when"] for x in cs[i:]]        # this call and every later one wait, in order
                self.waiting[why] += len(cs) - i
                break
            n = self.attempt(owner, c, start)
            lines.append({"call": c, "leads": leads, "start": start, "n": n,
                          "text": "%s: attempt %d, %s" % (_clock(c["when"], self.tz), n, out)})
        if lines:
            self.owners += 1
            left = self.write(owner, lines)
            if left:
                held = sorted(set(held) | {ln["call"]["when"] for ln in left})
        return held

    # ------------------------------------------------------------------ deciding one call
    def leads_at(self, rows: list[dict], when: datetime):
        """(the owner's houses that were leads when this call was made, when that lead began or None
        when it began before the call log here reaches), or None when no house was a lead."""
        found, starts = [], []
        for r in rows:
            u = str(r.get("uuid") or "")
            if not u:
                continue
            entries = _entries(u, when + self.grace + timedelta(seconds=1), self.leads)
            then = _status_at(u, lanes.status_of(r), when)
            if then in self.leads or any(when < t <= when + self.grace for t in entries):
                found.append(r)
                starts.append(entries[-1] if entries else None)
        if not found:
            return None
        return found, (None if any(s is None for s in starts) else min(starts))

    def outcome(self, c: dict) -> tuple[str | None, str]:
        """(the line's outcome, "") or (None, why the call cannot be told yet)."""
        st, now = self.st, self.ctx.now
        ans = c["answered"]
        if ans is None:
            if not c["noanswer"]:
                settle = timedelta(minutes=float(st["settle_minutes"]))
                if now - c["when"] < settle or self.caught_up < c["when"] + settle:
                    return None, "a call log not caught up with the call yet"
            out = "no answer"
        else:
            out, why = self.answered(c, ans)
            if out is None:
                return None, why
        disp = self.disposition(c)
        if disp:
            out += ", " + disp
        if ans and ans.get("caller") and st["caller_names"]:
            out += " (%s)" % ans["caller"]
        return out, ""

    def answered(self, c: dict, ans: dict) -> tuple[str | None, str]:
        st, now = self.st, self.ctx.now
        secs = ans.get("seconds") or 0
        young = now - c["when"] < timedelta(minutes=float(st["transcript_wait_minutes"]))
        text = transcript_of(c["call"])
        if text:
            kind = call_kind(text)
            if kind != "talk":
                return VOICE_OUT[kind], ""
            m = leadnotes.mark("call:%s" % c["call"], True) if c["call"] else {}
            if m.get("status") == "noted":
                return "talked %s, see the lead note" % _dur(secs), ""
            if m.get("status") in ("skipped", "closed") or not young:
                return "talked %s" % _dur(secs), ""
            return None, "a lead note still being written"
        if secs >= float(st["transcript_seconds"]) and ans.get("recording") and young:
            return None, "a transcript lead notes may still write"
        return "answered, %s" % _dur(secs), ""

    def disposition(self, c: dict) -> str:
        """What the caller or the dialer recorded about the number right after the call."""
        if not c["number"]:
            return ""
        until = c["when"] + timedelta(minutes=float(self.st["disposition_minutes"]))
        got = ""
        for e in activity.events("owner.phone.status.updated", resource=c["owner"], since=c["when"], until=until):
            d = e.get("data") or {}
            if str(d.get("number") or "") == c["number"]:
                got = DISPOSITIONS.get(str(d.get("to") or "").strip().upper(), got)
        return got

    def attempt(self, owner: str, c: dict, start: datetime | None) -> int:
        """1 + the calls to this owner since it became a lead (from as far back as the log copy reaches
        when the lead is older than that). A dial in the grace before the status counts: it made the lead."""
        since = (start - self.grace) if start else None
        evs = activity.events(CALL_TYPES, resource=owner, since=since, until=c["when"] + timedelta(seconds=1))
        earlier = [x for x in fold(evs, float(self.st["pair_minutes"])).values()
                   if x["key"] != c["key"] and (x["when"], x["key"]) < (c["when"], c["key"])]
        return len(earlier) + 1

    # ------------------------------------------------------------------ the gate
    def cleared(self, lines: list[dict]) -> tuple[list[dict], list[dict]]:
        """(the lines the gate lets this stage write, the lines that wait because a lead record could
        not be read this run). A line the gate holds is decided: it is marked and never written. The
        lines after one that waits wait too, so the log stays in order."""
        keep = []
        cap = int(self.st["max_records"])
        for i, ln in enumerate(lines):
            ok = True
            for r in ln["leads"][:cap]:
                u = str(r.get("uuid") or "")
                if u not in self.passed:
                    rec = self.record(u)
                    if rec is None:
                        return keep, lines[i:]
                    if _is_entity(rec.get("owner") or {}):
                        self.ctx.out.tally["skipped: a company or trust owns it (people only)"] += 1
                        self.passed[u] = False
                    else:
                        self.passed[u] = bool(self.ctx.passes(rec, "research"))
                ok = ok and self.passed[u]
            if ok:
                keep.append(ln)
            else:
                self.set_mark(ln["call"]["key"], "skipped", why="held back by the gate", owner=ln["call"]["owner"])
        return keep, []

    # ------------------------------------------------------------------ writing
    def write(self, owner: str, lines: list[dict]) -> list[dict]:
        """The lines onto the owner's one log. Returns the lines that were neither written nor
        decided (a record not read, a write that failed): they wait for the next run."""
        todo, waiting = self.cleared(lines)
        if not todo:
            return waiting
        streets = []
        for ln in todo:
            for r in ln["leads"]:
                s = addr(self.records.get(str(r.get("uuid") or "")) or r)
                if s not in streets:
                    streets.append(s)
        u = str(todo[0]["leads"][0].get("uuid") or "")
        if self.live:
            ok, new = self.write_live(owner, u, todo, streets)
        else:
            ok, new = self.write_plan(owner, todo, streets)
        if not ok:
            return todo + waiting
        for ln in todo:
            self.set_mark(ln["call"]["key"], "written", owner=owner, attempt=ln["n"], record=u)
        self.written.append((streets[0] if streets else owner[:8], [ln["text"].split(": ", 1)[-1] for ln in todo], new))
        return waiting

    def head_lines(self, todo: list[dict], streets: list[str], header: str) -> list[str]:
        """The top of a new log: what it is, which house, since when, and how many calls came before it."""
        first = todo[0]
        houses = " and ".join(streets[:2]) + (" and %d more" % (len(streets) - 2) if len(streets) > 2 else "")
        if first["start"] is not None:
            since = "since %s" % _day(first["start"], self.tz)
        else:
            if self.floor is None:
                oldest = activity.events(CALL_TYPES, limit=1)
                self.floor = oldest[0]["when"] if oldest else first["call"]["when"]
            since = "since before %s (as far back as the call log here reaches)" % _day(self.floor, self.tz)
        out = [header, "%s %s, a lead %s." % ("House:" if len(streets) == 1 else "Houses:", houses, since)]
        if first["n"] > 1:
            out.append("Earlier calls while a lead: %d, before this log began." % (first["n"] - 1))
        return out

    def body(self, todo: list[dict], since: str) -> tuple[list[str], str]:
        """(the new lines, the lead start they end on). A line of its own marks where the owner became
        a lead again, because the attempt count starts over there. `since` is "" for a new log,
        "before" for a lead that began before the call log here reaches, else the start's time."""
        out, last = [], since
        for ln in todo:
            s = ln["start"].isoformat(timespec="seconds") if ln["start"] is not None else "before"
            if last and s != "before" and (last == "before" or s > last):
                out.append("A lead again since %s: calls counted from 1." % _day(ln["start"], self.tz))
            if s != "before" or not last:
                last = s
            out.append(ln["text"])
        return out, last

    def write_plan(self, owner: str, todo: list[dict], streets: list[str]) -> tuple[bool, bool]:
        """Plan mode: the same decisions, nothing written. The plan's own memory of the log it would
        have started keeps the next plan run from starting it again."""
        plan = ledger.fact(owner, PLAN_KEY) or {}
        plan = plan if isinstance(plan, dict) else {}
        new = not plan.get("exists")
        if new and not self.free_to_pin(owner):
            self.unpinned.append(streets[0] if streets else owner[:8])
        self.ctx.out.writes += 1
        since = plan.get("since") or (todo[0]["start"].isoformat(timespec="seconds") if todo[0]["start"] else "")
        last = [ln["start"] for ln in todo if ln["start"] is not None]
        ledger.set_fact(owner, PLAN_KEY, {"exists": True, "since": max(last).isoformat(timespec="seconds") if last else since,
                                          "lines": int(plan.get("lines") or 0) + len(todo)})
        return True, new

    def free_to_pin(self, owner: str, exclude: str = "") -> bool:
        """True when nothing on the owner's board is pinned (other than `exclude`). Unknown (the read
        cap, a failed read) is not free: a second pinned note is worse than an unpinned log."""
        if not self._read():
            return False
        try:
            msgs = board_list(self.ctx.crm, owner, int(self.st["board_page"]))
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.out.problem("could not read an owner's message board before starting an attempt log (%s)" % str(exc)[:80])
            return False
        return not any(m.get("pinned") and str(m.get("uuid") or "") != exclude for m in msgs)

    def write_live(self, owner: str, u: str, todo: list[dict], streets: list[str]) -> tuple[bool, bool]:
        ctx = self.ctx
        fact = ledger.fact(owner, KEY) or {}
        fact = fact if isinstance(fact, dict) else {}
        street = streets[0] if streets else owner[:8]
        msg = None
        try:
            if fact.get("message"):
                if not self._read():
                    return False, False
                msg = board_get(ctx.crm, owner, fact["message"])
                if msg is None:
                    ctx.out.tally["a person deleted an attempt log, so a new one was started"] += 1
                elif fact.get("pinned") and not msg.get("pinned"):
                    ctx.out.tally["a person unpinned an attempt log, so a new one was started"] += 1
                    msg = None
            if msg is not None:
                current = str(msg.get("message") or "")
                added, last = self.body(todo, str(fact.get("since") or "before"))
                text = current.rstrip() + "\n" + "\n".join(added)
                if len(text) <= int(self.st["note_max"]):
                    return self.edit(owner, u, fact, msg, current, text, todo, street, last), False
                # Full: the old log is unpinned first (it is ours), then the log continues in a new note.
                if msg.get("pinned"):
                    ctx.change(u, WHAT, {"owner": owner, "message": msg.get("uuid"), "pinned": True},
                               {"owner": owner, "message": msg.get("uuid"), "pinned": False})
                    ctx.write("unpin full attempt log", board_unpin, ctx.crm, owner, str(msg.get("uuid")))
                return self.create(owner, u, todo, streets, CONTINUED, street), True
            return self.create(owner, u, todo, streets, HEADER, street), True
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (the calls stay open: the next run writes them)
            # One refusal stops the run's writes. The owner board's create route was unproven on the key
            # when this was written (its schema names an owner number the record never shows): if it
            # refuses one log it refuses them all, and one problem says so instead of one per lead.
            self.broken = True
            ctx.out.problem("could not write the attempt log on %s, so no more attempt logs were written this run (%s)"
                            % (street, str(exc)[:80]))
            return False, False

    def edit(self, owner: str, u: str, fact: dict, msg: dict, current: str, text: str, todo: list[dict],
             street: str, last: str) -> bool:
        ctx = self.ctx
        mid = str(msg.get("uuid") or fact.get("message") or "")
        ctx.change(u, WHAT, {"owner": owner, "message": mid, "text": current}, {"owner": owner, "message": mid, "text": text})
        ctx.write("attempt log line", board_patch, ctx.crm, owner, mid, text)
        pinned = bool(msg.get("pinned"))
        if not pinned and self.free_to_pin(owner, exclude=mid):
            # Left unpinned under a person's pinned note when it was started; that note is gone now.
            ctx.change(u, WHAT, {"owner": owner, "message": mid, "pinned": False}, {"owner": owner, "message": mid, "pinned": True})
            ctx.write("pin attempt log", board_pin, ctx.crm, owner, mid)
            pinned = True
        landed, actual = self.check(owner, mid, todo[-1]["text"], pinned, street)
        if not landed:
            return False
        self.remember(owner, mid, actual, todo, fact, last)
        ledger.event(ctx.run_id, ctx.stage.id, u, WHAT, {"owner": owner, "message": mid, "lines": len(todo)},
                     now=ctx.now, tz=self.tz)
        return True

    def create(self, owner: str, u: str, todo: list[dict], streets: list[str], header: str, street: str) -> bool:
        ctx = self.ctx
        added, last = self.body(todo, "")
        text = "\n".join(self.head_lines(todo, streets, header) + added)
        pin = self.free_to_pin(owner)
        ctx.change(u, WHAT, None, {"owner": owner, "text": text, "pinned": pin})
        r = ctx.write("start attempt log", board_post, ctx.crm, owner, text) or {}
        mid = str(r.get("uuid") or "")
        if not mid:
            self.broken = True
            ctx.out.problem("the message board answered with no note id when an attempt log was started on %s, so no "
                            "more attempt logs were written this run. Start one note by hand and compare" % street)
            return False
        ledger.event(ctx.run_id, ctx.stage.id, u, WHAT, {"owner": owner, "message": mid, "made": True, "lines": len(todo)},
                     now=ctx.now, tz=self.tz)
        if pin:
            ctx.write("pin attempt log", board_pin, ctx.crm, owner, mid)
        else:
            self.unpinned.append(street)
        landed, actual = self.check(owner, mid, todo[-1]["text"], pin, street, every=True)
        # Remembered even when the read-back failed: the next run finds this note by its id and adds
        # to it, instead of starting a second log on the same board.
        self.remember(owner, mid, actual, todo if landed else [], {}, last)
        return landed

    def check(self, owner: str, mid: str, last_line: str, pinned: bool, street: str,
              every: bool = False) -> tuple[bool, bool]:
        """The first live write of a run, and every new log, is read back: the note must hold the newest
        line, pinned as intended. Anything else stops every other attempt log this run. Returns (the
        newest line is on the board, pinned as read back). The pin is remembered AS READ: a pin route
        that answers and does nothing must not make the next run think a person unpinned the log and
        start another one on the same board."""
        if self.read_back and not every:
            return True, bool(pinned)
        self.read_back = True
        self.reads += 1
        try:
            got = board_get(self.ctx.crm, owner, mid) or {}
        except budget.BudgetExhausted:
            raise
        except Exception:  # noqa: BLE001  (a read that fails proves nothing: it is reported below)
            got = {}
        landed = last_line in str(got.get("message") or "")
        actual = bool(got.get("pinned"))
        if landed and actual == bool(pinned):
            return True, actual
        self.broken = True
        self.ctx.out.problem("the attempt log on %s did not read back as written (%s), so no more attempt logs were written "
                             "this run. Open the owner's message board and compare" % (
                                 street, "not pinned" if landed else "the new line is not there"))
        return landed, actual

    def remember(self, owner: str, mid: str, pinned: bool, todo: list[dict], fact: dict, last: str) -> None:
        """What this stage knows about the owner's log: the note, whether WE pinned it (so a log a
        person unpinned is told apart from one left unpinned under a person's note), and the lead
        start its last line was counted from."""
        calls = [str(x) for x in (fact.get("calls") or []) if fact.get("message") == mid] + [ln["call"]["key"] for ln in todo]
        ledger.set_fact(owner, KEY, {"message": mid, "pinned": bool(pinned), "since": last or "before", "calls": calls[-200:],
                                     "at": self.ctx.now.isoformat(timespec="seconds")})

    # ------------------------------------------------------------------ what it says
    def report(self) -> None:
        out = self.ctx.out
        n = sum(len(x[1]) for x in self.written)
        new = sum(1 for x in self.written if x[2])
        out.acted = n
        wait = sum(self.waiting.values())
        parts = []
        if n:
            parts.append("%s %s on %s (%d new)" % ("Wrote" if self.live else "Would write", _plural(n, "call line"),
                                                   _plural(len(self.written), "attempt log"), new))
        if wait:
            parts.append("%s for the next run" % ("1 call waits" if wait == 1 else "%d calls wait" % wait))
        out.head = ("; ".join(parts) + ".") if parts else ""
        for street, texts, _ in self.written[:6]:
            out.line(_clean("%s: %s" % (street, "; ".join(texts[:3]) + ("; and %d more" % (len(texts) - 3) if len(texts) > 3 else ""))))
        if self.unpinned:
            out.line("%s %s unpinned: a person's note is pinned there, and a lead never carries two pinned notes "
                     "(%s)" % (_plural(len(self.unpinned), "new attempt log"), "left" if self.live else "would be left",
                               "; ".join(self.unpinned[:3])))
        for why, k in self.waiting.most_common(2):
            out.line("Waiting: %s, held by %s." % (_plural(k, "call"), why))


def run_attemptlog(ctx) -> None:
    _Run(ctx).run()
