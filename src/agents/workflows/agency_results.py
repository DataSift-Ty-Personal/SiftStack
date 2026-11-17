"""agency_results: what the calling agency sends back, written onto each record by its Record ID.

The agency track sends a few thousand owners a month to an outside calling agency (autopilot/agencyexport.py),
and until this workflow nothing read what came back. An owner who told the agency's caller "remove me" never
reached our gate, so the same owner could later rise into the Top 1,000 and be texted by us; a number the
agency found wrong stayed callable; a lead the agency passed had to be typed in by hand. A person posts the
agency's per-record file in Slack (CSV or Excel, one row per number dialed, keyed on the Record ID our export
carries) and asks Quinn; this workflow reads every row and writes what the dialer dispositions lesson
teaches (L10.19 to L10.21, L14.56 to L14.59).

How it decides, and why each rule exists:
  * A ROW IS MATCHED BY ITS RECORD ID ONLY, never by address or name: two owners share a street, and a
    name match is how a stranger's house gets a stranger's answer. A row with no Record ID, one that is not
    a DataSift id, or one the account no longer holds goes to a person.
  * THE DISPOSITION DECIDES THE WRITE, from one table:
      remove me, do not call  the number dialed becomes do not call (DNC, or CORRECT_DNC and WRONG_DNC where
                              it was already judged: the texting app's own mapping), every house the owner
                              holds is tagged Do Not Market and put on the DNC list (Ty, 2026-10-28: every
                              stop lands on that list), and a note says why. Written first and always, even
                              before a large file's go and whatever the record's status: an owner who asked
                              to be removed is held by the gate from today (L10.8: never called again).
      lead                    New Lead, only over a status that still means prospecting (never over a lead
                              a person is working, never over a finished record), the tag Agency Lead, the
                              number marked correct and a note with the agency's notes, so speed to lead and
                              the lead clock pick it up.
      not interested          Not Interested over a prospecting status, and the number marked correct: the
                              disposition reached the owner (L10.19), unless the file says it did not.
      listed                  Listed, the number marked correct (L10.19).
      owner died              Verify Deceased and a note. Nobody is marked dead on a caller's word: the gate
                              then holds the record for research (the death screen's rule).
      wrong number, dead line that number's status only (L10.20), never over a number a person marked correct.
      no answer, voicemail    nothing: a number with no disposition keeps the record callable (L10.32).
      callback                nothing: the agency keeps its own callbacks; the date is in the file.
      sold, buyer and the like  known, but not this workflow's to write (the sold guard reads the deed):
                              listed for a person.
      anything else           listed for a person, never guessed. AGENTS_AGENCY_RESULTS_CODES maps an
                              agency's own codes onto these.
  * ONE HOUSE, SEVERAL ROWS, ONE OUTCOME. A removal outranks a lead (the removal is written, the lead goes
    to a person), a lead outranks a no from someone on another number, and a number one call reached and
    another called wrong goes to a person.
  * THE OWNER MUST STILL BE THE ONE CALLED, AND A PERSON. When the file carries the owner's last name and the
    record's owner now has another one (the house sold, the owner was replaced), no status and no tag is
    written: the number the person asked about is still marked, and the record goes to a person. A record a
    company owns gets no status either (people only, Ty 2026-11-14), but a removal on it is still written.
  * BOUNDED, REMOVALS FIRST. One run reads and writes at most AGENTS_AGENCY_RESULTS_MAX_RECORDS records,
    removals first, then leads; the rest continue in the background one run every
    AGENTS_AGENCY_RESULTS_GAP_S, so the one worker is never held for an hour and every run stays inside the
    account's request limit. A file that would write more than AGENTS_AGENCY_RESULTS_ASK_RECORDS records
    writes its removals now and the rest after a person says go.
  * LIVE ONLY, AND THE FILE CAN BE PUT BACK. On a dry box the first records are read and the answer says
    what a live run would write. Live, every change is recorded with the value it replaced (ledger.change)
    under ONE run id for the whole file, so `autopilot undo agency_results-<id>` puts it back; a note has no
    delete route, and the answer says so. The first record of every run is read back, and a write that did
    not stick stops the file.
  * THE SAME ANSWER IS NEVER WRITTEN TWICE. A file is known by its content, a record already written from it
    is skipped, and the same outcome arriving again in a corrected file is not written again.
  * ONLY THE PDF CAME BACK. The agency's monthly summary has no record ids, so nothing can be written: its
    numbers are read (pypdfium2 text), kept for the scorecard and shown as the agency's own, and the answer
    says plainly that nobody it was asked to remove can be held until the per-record file comes.
  * THE ANSWER CARRIES NO PHONE NUMBER AND NO NAME: counts and the leads by street in the thread; every
    record, and anything for a person, in two CSV files where a number shows its last four digits only.

Every parsed file is kept under DATA_DIR/agency_returns/ for the monthly scorecard (autopilot/agencyscore.py).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import attachments, config, slack_app, store
from ..autopilot import budget, gate, lanes, ledger
from ..autopilot.crm import titles
from . import quick_screen as qs

log = logging.getLogger(__name__)

WORKFLOW = "agency_results"
TZ = "America/New_York"
EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)     # written as code points: the house rule keeps the characters out
FACT_SENT = "agency_sent"     # agencyexport.FACT_SENT: the day a record was in a file the agency got
FACT_DONE = "agency_result"   # the outcomes already written on a record, so a later file never writes one twice
CSV_ALL = "agency_results.csv"
CSV_PERSON = "agency_results_for_a_person.csv"
TEXT_PDF = "agency_report_text.txt"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TABLE_EXT = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm"}
PDF_EXT = {".pdf"}
GO_WORDS = {"yes", "y", "go", "go ahead", "true", "1", "ok", "okay", "write it", "write them", "write all"}
HEADER_ROWS = 15              # the header row is looked for this far down (a title line or two above it is common)
PERSON_ROWS = 5000            # rows the for-a-person file holds at most

# Stand-ins the selftest sets, so no test reaches Slack or DataSift.
FILES = None      # (channel, thread) -> file briefs shaped like attachments.on_thread
FETCH = None      # (file brief) -> the file's bytes
CRM = None        # the budgeted DataSift client (agents.autopilot.crm.CRM) on the Open API key
NOW = None        # a fixed "now"
ENQUEUE = None    # store.enqueue
PDF_TEXT = None   # (path) -> one string per page
POLICY = None     # the playbook: its gate block names the suppression tags and lists

ASK = ("one row for every number dialed, with the Record ID from the file we sent, the number dialed, the "
       "disposition (wrong number, disconnected, do not call or remove, not interested, lead, callback, no answer), "
       "the call date, any callback date and the caller's notes, as a CSV or Excel file")

# ---------------------------------------------------------------------- what a disposition means
RECORD_CLASSES = ("remove", "lead", "deceased", "listed", "not_interested")   # each outranks the ones after it
PHONE_CLASSES = ("wrong", "dead")
QUIET = ("callback", "no_contact", "blank")       # nothing to write: the record stays callable (L10.32)
CLASSES = RECORD_CLASSES + PHONE_CLASSES + QUIET + ("person", "unknown")
ORDER = {c: i for i, c in enumerate(RECORD_CLASSES + PHONE_CLASSES)}
REACHED = {"lead", "listed", "not_interested"}

EXACT = {
    # remove me
    "dnc": "remove", "do not call": "remove", "dont call": "remove", "don t call": "remove", "remove": "remove",
    "remove me": "remove", "removed": "remove", "remove from list": "remove", "take off list": "remove",
    "take me off": "remove", "take me off the list": "remove", "stop": "remove", "stop calling": "remove",
    "opt out": "remove", "opted out": "remove", "do not contact": "remove", "litigator": "remove",
    # lead
    "lead": "lead", "hot lead": "lead", "warm lead": "lead", "hot": "lead", "warm": "lead", "interested": "lead",
    "appointment": "lead", "appointment set": "lead", "appt": "lead", "appt set": "lead", "transfer": "lead",
    "transferred": "lead", "live transfer": "lead", "qualified": "lead", "qualified lead": "lead",
    "motivated": "lead", "motivated seller": "lead", "wants offer": "lead", "wants an offer": "lead", "yes": "lead",
    # not interested
    "ni": "not_interested", "not interested": "not_interested", "no interest": "not_interested",
    "not selling": "not_interested", "not for sale": "not_interested", "not looking to sell": "not_interested",
    "no thanks": "not_interested", "declined": "not_interested", "keeping": "not_interested",
    # listed
    "listed": "listed", "listed with realtor": "listed", "listed with agent": "listed", "has realtor": "listed",
    "has a realtor": "listed", "has an agent": "listed", "with realtor": "listed", "on the market": "listed",
    # the owner died
    "deceased": "deceased", "owner deceased": "deceased", "passed away": "deceased", "owner passed": "deceased",
    "owner passed away": "deceased", "owner died": "deceased",
    # the number
    "wrong number": "wrong", "wrong": "wrong", "wrong person": "wrong", "wrong party": "wrong", "wn": "wrong",
    "not owner": "wrong", "not the owner": "wrong", "tenant": "wrong", "renter": "wrong",
    "disconnected": "dead", "dead": "dead", "dead line": "dead", "dead number": "dead", "bad number": "dead",
    "invalid": "dead", "invalid number": "dead", "not in service": "dead", "out of service": "dead", "fax": "dead",
    "fax machine": "dead", "no longer in service": "dead", "disc": "dead",
    # nothing to write
    "callback": "callback", "call back": "callback", "call back later": "callback", "callback scheduled": "callback",
    "follow up": "callback", "followup": "callback", "call later": "callback", "cb": "callback",
    "no answer": "no_contact", "na": "no_contact", "voicemail": "no_contact", "vm": "no_contact",
    "voice mail": "no_contact", "left voicemail": "no_contact", "left message": "no_contact", "lvm": "no_contact",
    "busy": "no_contact", "hung up": "no_contact", "hangup": "no_contact", "no contact": "no_contact",
    "answering machine": "no_contact", "machine": "no_contact", "rna": "no_contact", "ring no answer": "no_contact",
    "not available": "no_contact", "unavailable": "no_contact", "dropped": "no_contact", "dropped call": "no_contact",
    "abandoned": "no_contact", "no response": "no_contact", "language barrier": "no_contact", "spanish": "no_contact",
    # known, for a person
    "sold": "person", "already sold": "person", "house sold": "person", "under contract": "person",
    "buyer": "person", "investor": "person", "business": "person", "commercial": "person",
}
# A disposition that is not one of the codes above is read by these, first match wins. A removal comes first
# (a "not interested, remove me" is a removal) and "not interested" before "interested".
RULES = [
    ("remove", re.compile(r"\b(do not call|dont call|don t call|dnc|remove|removed|take (me )?off|stop calling|"
                          r"opt(ed)? out|never call|litigat)")),
    ("deceased", re.compile(r"\b(deceased|passed away|owner passed|owner died|died)\b")),
    ("not_interested", re.compile(r"\b(not interested|no interest|not selling|not for sale|not looking to sell|"
                                  r"wont sell|won t sell|no thanks)\b")),
    ("dead", re.compile(r"\b(disconnected|not in service|out of service|invalid|bad number|fax|dead line|"
                        r"dead number|no longer in service)\b")),
    ("wrong", re.compile(r"\b(wrong|not (the )?owner|tenant|renter)\b")),
    ("lead", re.compile(r"\b(lead|interested|appointment|appt|transfer(red)?|qualified|motivated|wants (an )?offer)\b")),
    ("listed", re.compile(r"\b(listed|realtor|on the market)\b")),
    ("person", re.compile(r"\b(sold|under contract|buyer|investor)\b")),
    ("callback", re.compile(r"\b(call ?back|follow ?up|call later)\b")),
    ("no_contact", re.compile(r"\b(no answer|voice ?mail|vm|busy|hung up|hangup|machine|left message|no contact)\b")),
]
# "not a lead", "no lead", "unqualified": a negated lead word is never read as a lead.
NEGATED = re.compile(r"\b(not|no|non|un) ?(a )?(lead|qualified|motivated)\b")

# The columns, by the names an agency's export uses. The first name in each list that a file carries wins,
# so a file with both a Disposition and a Status column reads its Disposition.
FIELDS = {
    "record": ("record id", "record_id", "recordid", "record uuid", "datasift record id", "datasift id",
               "sift record id", "sift id", "record"),
    "disposition": ("disposition", "dispo", "call disposition", "last disposition", "disposition name",
                    "disposition code", "call result", "result", "outcome", "call outcome", "status", "call status",
                    "lead status"),
    "phone": ("phone dialed", "number dialed", "dialed number", "phone number dialed", "dialed", "phone",
              "phone number", "number", "called number", "contact number"),
    "notes": ("notes", "note", "agent notes", "call notes", "comments", "comment", "remarks", "lead notes",
              "summary", "details"),
    "callback": ("callback date", "call back date", "callback", "call back", "callback time", "follow up date",
                 "followup date", "follow up", "appointment date", "appointment", "appt date"),
    "call_date": ("call date", "date called", "called on", "dial date", "call time", "last call date",
                  "last called", "date", "timestamp", "date time"),
    "reached": ("right party", "right party contact", "rpc", "owner reached", "reached", "spoke with owner",
                "spoke to owner", "contact type", "spoke with", "person reached"),
    "last": ("last name", "owner last name", "lastname", "last name 1"),
    "address": ("property address", "address", "street", "property street"),
    "agent": ("agent", "agent name", "caller", "rep", "called by"),
}
# The wide shape: our own export with a disposition beside each Phone N ("Phone 2 Disposition").
WIDE_DISP = re.compile(r"^phone (\d{1,2}) (disposition|dispo|status|result|outcome|call result)$")
YES_WORDS = {"yes", "y", "true", "1", "owner", "right party", "rpc", "self", "homeowner", "owner reached"}
NO_WORDS = {"no", "n", "false", "0", "wrong party", "not owner", "spouse", "relative", "family", "tenant", "other"}

# The phone statuses DataSift takes (the published Phone schema).
DNC_FAMILY = {"DNC", "CORRECT_DNC", "WRONG_DNC"}
DNC_FOR = {"CORRECT": "CORRECT_DNC", "WRONG": "WRONG_DNC"}     # the texting app's own mapping (sms_agent.crm)
UNSET = {"", "UNKNOWN", "NO_ANSWER"}
HARD = ("do_not_market", "litigator", "opted_out", "sold")        # what the gate lets nothing lift

MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
MONTH_RX = re.compile(r"\b(january|february|march|april|june|july|august|september|october|november|december|"
                      r"(?:may|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)(?=\.?\s*\d{4}))\b\.?\s*(\d{4})?", re.I)


# ---------------------------------------------------------------------- text helpers
def norm(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def clean(text, keep: int = 160) -> str:
    """One line, no long or short dash, at most `keep` characters."""
    t = " ".join(str(text or "").replace(EM_DASH, "-").replace(EN_DASH, "-").split())
    return t[:keep]


def ends(number: str) -> str:
    return number[-4:] if number else ""


def _n(n, one: str, many: str = "") -> str:
    n = int(n or 0)
    return "%s %s" % (format(n, ","), one if n == 1 else (many or one + "s"))


# ---------------------------------------------------------------------- settings
def _words(v) -> list[str]:
    raw = str(v or "").strip()
    if raw.lower() == "none":
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _statuses(v) -> set[str]:
    return {x.lower() for x in _words(v)} | {""}


def codes(raw) -> tuple[dict, list[str]]:
    """An agency's own disposition codes onto this workflow's classes, as JSON ({"NI2": "not_interested"}) or
    code=class pairs apart by commas. Returns (the map, the pairs that name no class)."""
    text = str(raw or "").strip()
    if not text:
        return {}, []
    if text.startswith("{"):
        try:
            pairs = list(json.loads(text).items())
        except (ValueError, AttributeError):
            return {}, ["AGENTS_AGENCY_RESULTS_CODES is not a JSON object"]
    else:
        pairs = [p.split("=", 1) for p in text.split(",") if "=" in p]
    out, bad = {}, []
    for k, v in pairs:
        cls = str(v or "").strip().lower()
        if cls not in CLASSES:
            bad.append("AGENTS_AGENCY_RESULTS_CODES maps %s to %s, which is not one of %s" % (
                str(k).strip(), cls or "nothing", ", ".join(CLASSES)))
            continue
        out[norm(k)] = cls
    return out, bad


def settings() -> dict:
    g = qs.setting
    mapped, bad = codes(g("AGENTS_AGENCY_RESULTS_CODES", ""))
    return {
        "max_records": max(1, g("AGENTS_AGENCY_RESULTS_MAX_RECORDS", 80)),
        "ask_records": max(0, g("AGENTS_AGENCY_RESULTS_ASK_RECORDS", 250)),
        "gap_s": max(10, g("AGENTS_AGENCY_RESULTS_GAP_S", 60)),
        "wait_s": max(60, g("AGENTS_AGENCY_RESULTS_WAIT_S", 900)),
        "max_runs": max(1, g("AGENTS_AGENCY_RESULTS_MAX_RUNS", 120)),
        "max_rows": max(1, g("AGENTS_AGENCY_RESULTS_MAX_ROWS", 60000)),
        "max_houses": max(1, g("AGENTS_AGENCY_RESULTS_MAX_HOUSES", 20)),
        "lead_tag": str(g("AGENTS_AGENCY_RESULTS_LEAD_TAG", "Agency Lead")).strip(),
        "deceased_tag": str(g("AGENTS_AGENCY_RESULTS_DECEASED_TAG", "Verify Deceased")).strip(),
        "remove_lists": _words(g("AGENTS_AGENCY_RESULTS_REMOVE_LISTS", "DNC")),
        "all_houses": g("AGENTS_AGENCY_RESULTS_ALL_HOUSES", True),
        "ni_marks_correct": g("AGENTS_AGENCY_RESULTS_NI_CORRECT", True),
        "lead_over": _statuses(g("AGENTS_AGENCY_RESULTS_LEAD_OVER",
                                 "prospecting,not_interested,exhausted,deep prospecting,return mail")),
        "ni_over": _statuses(g("AGENTS_AGENCY_RESULTS_NI_OVER", "prospecting,exhausted")),
        "codes": mapped, "code_problems": bad,
        "ask": str(g("AGENTS_AGENCY_RESULTS_ASK", ASK)).strip() or ASK,
    }


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _policy():
    """The playbook for the gate's tags and lists, its zone and its own working statuses. A playbook that will
    not load must not stop a removal from being written: the gate's defaults apply."""
    if POLICY is not None:
        return POLICY
    try:
        from ..autopilot import policy as policy_mod
        return policy_mod.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("playbook not loaded for agency results: %s", exc)
        return None


def crm():
    return CRM if CRM is not None else qs.crm()


def classify(text, extra: dict | None = None) -> str:
    """The class of one disposition as the agency wrote it. Empty is blank; anything the table does not know
    is unknown, never guessed."""
    t = norm(text)
    if not t:
        return "blank"
    if extra and t in extra:
        return extra[t]
    if t in EXACT:
        return EXACT[t]
    if NEGATED.search(t):
        return "unknown"
    for cls, rx in RULES:
        if rx.search(t):
            return cls
    return "unknown"


def as_day(v) -> str:
    """A date in any of the shapes a dialer exports, as YYYY-MM-DD, or ''."""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    t = str(v or "").strip()
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", t)
        if m:
            y = int(m.group(3))
            return date(y + 2000 if y < 100 else y, int(m.group(1)), int(m.group(2))).isoformat()
        m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", t)
        if m:
            k = next((i for i, full in enumerate(MONTHS) if full.startswith(m.group(1).lower()[:3])), None)
            if k is not None:
                return date(int(m.group(3)), k + 1, int(m.group(2))).isoformat()
    except ValueError:
        return ""
    return ""


def reached_of(v):
    t = norm(v)
    if t in YES_WORDS:
        return True
    if t in NO_WORDS:
        return False
    return None


def name_tokens(text) -> set[str]:
    stop = {"jr", "sr", "ii", "iii", "iv", "the", "and", "of", "estate", "trust", "etal", "et", "al", "mr", "mrs", "ms"}
    return {w for w in re.findall(r"[a-z]{2,}", str(text or "").lower()) if w not in stop}


# ---------------------------------------------------------------------- the files on the thread
def _safe_name(name: str) -> str:
    stem, suf = Path(name).stem, Path(name).suffix.lower()
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") or "file")[:50] + suf[:8]


def download(f: dict) -> tuple[bytes, str]:
    """(the file's bytes, "") or (b"", why not). The attachments module saves photos, PDFs and text for an
    employee's turn; an Excel file is none of those, so the workflow saves the agency's file itself."""
    if FETCH is not None:
        try:
            return FETCH(f), ""
        except Exception as exc:  # noqa: BLE001
            return b"", "it could not be saved (%s)" % type(exc).__name__
    if not slack_app.can_read_files():
        return b"", attachments.SCOPE_HELP
    if f.get("needs_info"):
        full = slack_app.file_info(f.get("id") or "")
        if full:
            f = attachments.brief(full, f.get("user", ""), f.get("ts", ""))
    url = f.get("url") or ""
    if not url:
        return b"", "Slack gave no link to it"
    try:
        r = slack_app.fetch(url)
    except Exception as exc:  # noqa: BLE001
        return b"", "Slack could not be reached (%s)" % type(exc).__name__
    if getattr(r, "status_code", 200) != 200:
        return b"", "Slack answered %s" % getattr(r, "status_code", "?")
    if "text/html" in str((getattr(r, "headers", None) or {}).get("content-type") or "").lower():
        slack_app.forget_scopes()
        return b"", attachments.SCOPE_HELP
    data = getattr(r, "content", b"") or b""
    if len(data) > config.MAX_FILE_MB * 1024 * 1024:
        return b"", "it is larger than %d MB" % int(config.MAX_FILE_MB)
    if not data:
        return b"", "Slack sent an empty file"
    return data, ""


def thread_files(task: dict, folder: Path) -> tuple[list[dict], list[dict]]:
    """(the CSV, Excel and PDF files on this thread, saved in the run folder; the ones that could not be saved,
    with why). A photo or a boundary on the same thread is not the agency's file and is passed over."""
    channel, thread = task.get("slack_channel") or "", task.get("slack_thread") or ""
    try:
        briefs = (FILES or attachments.on_thread)(channel, thread)
    except Exception as exc:  # noqa: BLE001
        return [], [{"name": "the files on this thread", "why": "they could not be listed (%s)" % type(exc).__name__}]
    inbox = folder.parent / "inbox"
    usable, skipped = [], []
    for i, f in enumerate(briefs or [], 1):
        name = str(f.get("name") or "file")
        ext = Path(name).suffix.lower()
        kind = "table" if ext in TABLE_EXT else "pdf" if ext in PDF_EXT else ""
        if not kind:
            continue
        dest = inbox / ("%02d-%s" % (i, _safe_name(name)))
        if not (dest.is_file() and dest.stat().st_size > 0):
            data, why = download(f)
            if why:
                skipped.append({"name": name, "why": why})
                continue
            inbox.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        usable.append({"name": name, "kind": kind, "path": dest})
    return usable, skipped


def pick(files: list[dict], wanted) -> tuple[list[dict], str]:
    names = [w.strip().lower() for w in str(wanted or "").split(",") if w.strip()]
    if not names:
        return files, ""
    out = []
    for n in names:
        hit = [f for f in files if n in (f["name"].lower(), Path(f["name"]).stem.lower())]
        if not hit:
            return [], "no file named %s is on this thread" % n
        out += [f for f in hit if f not in out]
    return out, ""


# ---------------------------------------------------------------------- reading a table
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")[:19]
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))            # a phone number Excel stored as a number
    return str(v).strip()


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def tables_in(path: Path) -> list[tuple[str, list[list[str]]]]:
    """(sheet name, rows) for every table in the file: each sheet of a workbook, or the one CSV."""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(str(path), read_only=True, data_only=True)
        try:
            return [(ws.title, [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
        finally:
            wb.close()
    text = _decode(path.read_bytes())
    if ext == ".tsv":
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
        except csv.Error:
            delim = ","
    return [("", [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), delimiter=delim)])]


def find_columns(header: list) -> dict:
    names = [norm(h) for h in header]
    cols: dict = {}
    for field, syns in FIELDS.items():
        for s in syns:
            if s in names:
                cols[field] = names.index(s)
                break
    wide = {}
    for i, n in enumerate(names):
        m = WIDE_DISP.match(n)
        if m and ("phone %s" % m.group(1)) in names:
            wide[int(m.group(1))] = (names.index("phone %s" % m.group(1)), i)
    cols["wide"] = wide
    return cols


def header_of(table: list[list]) -> tuple[int | None, dict, str]:
    """(the header row's index, its columns, "") or (None, {}, why the table cannot be read)."""
    seen_record = seen_disp = False
    for i, row in enumerate(table[:HEADER_ROWS]):
        cols = find_columns(row)
        has_disp = "disposition" in cols or bool(cols["wide"])
        if "record" in cols and has_disp:
            return i, cols, ""
        seen_record |= "record" in cols
        seen_disp |= has_disp
    if seen_disp and not seen_record:
        return None, {}, ("it has no Record ID column, so no row can be matched to a record (rows are matched by "
                          "Record ID only, never by address or name)")
    if seen_record:
        return None, {}, "it has a Record ID column but no disposition column"
    return None, {}, "no row near the top names a Record ID and a disposition"


def rows_from(table: list[list], hi: int, cols: dict, name: str, extra: dict) -> list[dict]:
    """One row per disposition: a file with a disposition beside each Phone N gives one row per number."""
    def at(raw, key):
        i = cols.get(key)
        return raw[i] if i is not None and i < len(raw) else ""

    out = []
    for k, raw in enumerate(table[hi + 1:], start=hi + 2):
        if not any(str(c or "").strip() for c in raw):
            continue
        rid = str(at(raw, "record") or "").strip().strip("{}\"'").lower()
        base = {"row": k, "file": name, "record": rid, "notes": clean(at(raw, "notes"), 600),
                "callback": as_day(at(raw, "callback")) or clean(at(raw, "callback"), 40),
                "call_date": as_day(at(raw, "call_date")), "reached": reached_of(at(raw, "reached")),
                "last": clean(at(raw, "last"), 60), "address": clean(at(raw, "address"), 100),
                "agent": clean(at(raw, "agent"), 40)}
        per_phone = []
        for _, (pi, di) in sorted(cols["wide"].items()):
            d = raw[di] if di < len(raw) else ""
            if str(d or "").strip():
                per_phone.append((clean(d, 80), gate.digits(raw[pi] if pi < len(raw) else "")))
        if "disposition" in cols:
            d = clean(at(raw, "disposition"), 80)
            if d or not per_phone:
                per_phone.insert(0, (d, gate.digits(at(raw, "phone"))))
        for d, ph in per_phone:
            row = dict(base, disposition=d, phone=ph)
            row["class"] = classify(d, extra)
            out.append(row)
    return out


def parse_file(f: dict, extra: dict) -> tuple[list[dict], str]:
    """(rows, "") or ([], why the file cannot be read). Each sheet of a workbook is tried in turn."""
    try:
        tables = tables_in(Path(f["path"]))
    except Exception as exc:  # noqa: BLE001  (a damaged or locked file is one file for a person, not a crash)
        return [], "it could not be opened (%s)" % type(exc).__name__
    why = "it is empty"
    for label, table in tables:
        hi, cols, why = header_of(table)
        if hi is None:
            continue
        rows = rows_from(table, hi, cols, f["name"] + (" (%s)" % label if label and len(tables) > 1 else ""), extra)
        if rows:
            return rows, ""
        why = "it has the right columns but no rows under them"
    return [], why


# ---------------------------------------------------------------------- one house's rows, one outcome
def outcome(rows: list[dict], ni_correct: bool = True) -> dict:
    """The record-level class that outranks the rest, what each number dialed becomes, and anything that
    needs a person."""
    classes = [r["class"] for r in rows]
    top = next((c for c in RECORD_CLASSES if c in classes), "") or next((c for c in PHONE_CLASSES if c in classes), "")
    person = []
    if top == "remove" and "lead" in classes:
        person.append("one call asked to be removed and another passed a lead; only the removal was written")
    for r in rows:
        if r["class"] == "unknown":
            person.append("a disposition I do not know: %s" % (r["disposition"] or "empty"))
        elif r["class"] == "person":
            person.append("the agency was told: %s" % r["disposition"])
    numbers: dict = {}
    for r in rows:
        if r.get("phone"):
            numbers.setdefault(r["phone"], []).append(r)
    phones = {}
    for n, rs in numbers.items():
        cs = {r["class"] for r in rs}
        reached_rows = [r for r in rs if r["class"] in REACHED]
        if "remove" in cs:
            phones[n] = "dnc"
        elif reached_rows and cs & set(PHONE_CLASSES):
            phones[n] = "conflict"
            person.append("the number ending %s reached the owner on one call and was called %s on another" % (
                ends(n), "a wrong number" if "wrong" in cs else "disconnected"))
        elif reached_rows:
            said_no = all(r.get("reached") is False for r in reached_rows)
            only_ni = {r["class"] for r in reached_rows} == {"not_interested"}
            if not said_no and (ni_correct or not only_ni):
                phones[n] = "correct"
        elif "dead" in cs:
            phones[n] = "dead"
        elif "wrong" in cs:
            phones[n] = "wrong"
    if "deceased" in classes and top != "lead":
        # The number reached somebody who said the owner died: it is a way to the family, not the owner.
        phones = {n: k for n, k in phones.items() if k != "correct"}
    # Every call that reached the owner sits on a number another call called wrong: whether the owner was
    # reached at all is in doubt, so the house gets no status.
    reached_all = [r for r in rows if r["class"] in REACHED]
    hold = ""
    if top in REACHED and reached_all and all(r.get("phone") and phones.get(r["phone"]) == "conflict" for r in reached_all):
        hold = "the calls on this house disagree"
    return {"top": top, "phones": phones, "deceased": "deceased" in classes, "person": list(dict.fromkeys(person)),
            "hold": hold}


def outcome_key(o: dict, rows: list[dict]) -> str:
    calls = sorted(r.get("call_date") or "" for r in rows)
    raw = "%s|%s|%s|%s" % (o["top"], sorted(o["phones"].items()), o["deceased"], calls[-1] if calls else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------- the stored return
def returns_dir() -> Path:
    return Path(str(qs.setting("AGENTS_AGENCY_RETURNS_DIR", str(Path(config.DATA_DIR) / "agency_returns"))))


def _path(rid: str) -> Path:
    return returns_dir() / ("%s.json" % re.sub(r"[^a-z0-9-]", "", str(rid).lower()))


def save_return(ret: dict) -> None:
    p = _path(ret["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(ret, default=str), encoding="utf-8")
    tmp.replace(p)


def load_return(rid: str) -> dict | None:
    try:
        v = json.loads(_path(rid).read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else None
    except (OSError, ValueError):
        return None


def load_returns() -> list[dict]:
    """Every parsed return on the box, oldest first (the monthly scorecard reads these)."""
    d = returns_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(v, dict) and v.get("id"):
            out.append(v)
    return sorted(out, key=lambda r: str(r.get("received") or ""))


def return_id(kind: str, parts: list[str]) -> str:
    h = hashlib.sha1(kind.encode("utf-8"))
    for p in parts:
        h.update(b"\n" + str(p).encode("utf-8"))
    return h.hexdigest()[:12]


def month_of(given: str, dates: list[str], texts: list[str], posted: datetime, tz: str) -> tuple[str, str]:
    """(YYYY-MM the agency called in, what said so). The ask's month wins, then the file's own call dates,
    then a month named in the file, then the day it was posted (in the first ten days of a month, the month
    before: the agency reports a finished month)."""
    if given:
        return given, "the ask"
    months = Counter(d[:7] for d in dates if d)
    if months:
        return months.most_common(1)[0][0], "the call dates"
    local = posted.astimezone(ZoneInfo(tz)).date()
    found = Counter()
    for t in texts:
        for m in MONTH_RX.finditer(t or ""):
            k = next((i for i, full in enumerate(MONTHS) if full.startswith(m.group(1).lower()[:3])), None)
            if k is None:
                continue
            year = int(m.group(2)) if m.group(2) else (local.year if k + 1 <= local.month else local.year - 1)
            found["%04d-%02d" % (year, k + 1)] += 1
    if found:
        return found.most_common(1)[0][0], "the month named in the file"
    first = local.replace(day=1)
    if local.day <= 10:
        first = (first - timedelta(days=1)).replace(day=1)
    return first.strftime("%Y-%m"), "the day it was posted"


def _slim(r: dict) -> dict:
    return {k: r.get(k) for k in ("row", "file", "disposition", "class", "phone", "notes", "callback", "call_date",
                                  "reached", "last", "address", "agent")}


def build_return(rows: list[dict], files: list[str], month: str, how: str, task: dict, now: datetime,
                 s: dict, tz: str) -> dict:
    rid = return_id("records", sorted("%s|%s|%s|%s" % (r["record"], r["disposition"], r["phone"], r["call_date"])
                                      for r in rows))
    by_record: dict = {}
    unmatched, quiet_bad = [], Counter()
    for r in rows:
        u = r["record"]
        if not UUID_RE.match(u):
            if r["class"] in QUIET:
                quiet_bad[r["class"]] += 1      # nothing to write: counted, not listed one by one
            else:
                unmatched.append({"row": r["row"], "file": r["file"], "record": u[:40], "street": r["address"],
                                  "disposition": r["disposition"], "number": ends(r["phone"]),
                                  "why": "no Record ID" if not u else "not a DataSift record id"})
            continue
        by_record.setdefault(u, []).append(r)
    records = {}
    for u, rs in by_record.items():
        o = outcome(rs, bool(s["ni_marks_correct"]))
        needs = o["top"] in RECORD_CLASSES or bool(o["phones"])
        records[u] = {"top": o["top"], "phones": o["phones"], "deceased": o["deceased"], "person": o["person"],
                      "hold": o["hold"], "rows": [_slim(r) for r in rs], "key": outcome_key(o, rs),
                      "state": "pending" if needs else ("person" if o["person"] else "nothing"), "why": "",
                      "street": next((r["address"] for r in rs if r["address"]), ""), "wrote": [], "dry": None}
    to_write = sum(1 for e in records.values() if e["state"] == "pending")
    return {"id": rid, "kind": "records", "received": now.isoformat(timespec="seconds"),
            "day": now.astimezone(ZoneInfo(tz)).date().isoformat(), "month": month, "month_from": how,
            "files": files, "task": int(task.get("id") or 0), "rows": len(rows),
            "row_counts": dict(Counter(r["class"] for r in rows)), "quiet_bad": dict(quiet_bad), "records": records,
            "unmatched": unmatched, "go": False, "needs_go": to_write > int(s["ask_records"]), "done": False,
            "runs": 0, "chain": 0, "stopped": "", "written": 0, "run_id": "%s-%s" % (WORKFLOW, rid)}


def tally(ret: dict) -> Counter:
    """What the agency's file says, counted the way the answer and the scorecard say it: a house once for its
    outcome, a number once for wrong or disconnected, a row once for the dispositions that write nothing."""
    c = Counter()
    for e in (ret.get("records") or {}).values():
        if e["top"] in RECORD_CLASSES and e["top"] != "deceased":
            c[e["top"]] += 1
        if e.get("deceased"):
            c["deceased"] += 1
        for cls in PHONE_CLASSES:
            c[cls] += len({r.get("phone") or "row%s" % r.get("row") for r in e["rows"] if r["class"] == cls})
    for cls in QUIET + ("person", "unknown"):
        c[cls] = int((ret.get("row_counts") or {}).get(cls, 0))
    return c


# ---------------------------------------------------------------------- one record: what would be written
def _raw_status(rec: dict) -> str:
    s = rec.get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "")


def street_of(rec: dict) -> str:
    a = rec.get("address") or {}
    return ", ".join(p for p in (a.get("street"), a.get("city")) if p) if isinstance(a, dict) else str(a)


def hard_hold(rec: dict, cfg) -> str:
    """Do Not Market, a litigator, an opt-out or a sale: what the gate lets nothing lift. No lead and no status
    is written on such a record."""
    allow = {lift for _, lift in gate.RULES.values() if lift} | {"not_interested", "deceased"}
    v = gate.check(rec, "research", cfg=cfg, allow=allow)
    for code, detail in v.reasons:
        if code in HARD:
            t = gate.TEXT[code]
            return t % detail if "%s" in t else t
    return ""


def is_company(owner: dict) -> bool:
    from deep_prospect_batch import is_entity       # the shared people-only test (Ty, 2026-11-14)
    return is_entity(owner or {})


def owner_changed(rows: list[dict], owner: dict) -> bool:
    """True when the file names the owner it called and the record's owner now has none of those names."""
    named = set()
    for r in rows:
        named |= name_tokens(r.get("last"))
    if not named:
        return False
    have = name_tokens(owner.get("last_name")) | name_tokens(owner.get("first_name")) | name_tokens(owner.get("company"))
    return bool(have) and not (named & have)


def _notes(rows: list[dict]) -> str:
    return clean("; ".join(dict.fromkeys(r["notes"] for r in rows if r.get("notes"))), 600)


def _call(rows: list[dict], cls: str) -> str:
    """'call on 2026-11-03, number ending 0101, caller Sam' for the first row of this class."""
    r = next((x for x in rows if x["class"] == cls), rows[0])
    parts = []
    if r.get("call_date"):
        parts.append("call on %s" % r["call_date"])
    if r.get("phone"):
        parts.append("number ending %s" % ends(r["phone"]))
    if r.get("agent"):
        parts.append("caller %s" % r["agent"])
    return ", ".join(parts) or "no call date in the file"


def note_for(e: dict, plan: dict, s: dict, dnm: str) -> str:
    """The note left on the record, in plain words. The agency's own notes go here and nowhere else."""
    rows, top = e["rows"], e["top"]
    call, said = _call(rows, top), _notes(rows)
    if top == "remove":
        if plan["changed"]:
            return ("The calling agency's caller was asked to stop calling (%s). That number is marked do not call. "
                    "The owner on this record has changed since the file was sent, so the record itself was not "
                    "tagged." % call)
        lists = (" and put on the %s list" % " and ".join(plan["remove_lists"])) if plan["remove_lists"] else ""
        return ("The calling agency's caller was asked to remove this owner (%s). Tagged %s%s, so nobody here calls, "
                "texts or mails them again." % (call, dnm, lists))
    if top == "lead":
        out = "Lead from the calling agency (%s)%s." % (call, (": %s" % said) if said else "")
        back = next((r["callback"] for r in rows if r["class"] == "lead" and r.get("callback")), "")
        if back:
            out += " Callback set for %s." % back
        if plan["kept_lead"]:
            out += " The record was already a lead, so its status stayed."
        if e.get("deceased"):
            out += (" The agency was also told the owner died: tagged %s. Confirm who you are talking to before "
                    "you talk about the house." % s["deceased_tag"])
        return out
    if top == "deceased":
        return ("The calling agency was told the owner died (%s)%s. Nobody is marked dead on a caller's word: tagged %s "
                "so research checks first." % (call, (": %s" % said) if said else "", s["deceased_tag"]))
    if top == "listed":
        return "The calling agency was told the house is listed with an agent (%s)%s." % (call, (": %s" % said) if said else "")
    if top == "not_interested" and said:
        return "The calling agency was told not interested (%s): %s." % (call, said)
    return ""


def _blank_plan() -> dict:
    return {"status": "", "tags": [], "lists": [], "house_tags": [], "house_lists": [], "phones": [], "note": "",
            "person": [], "what": [], "info": [], "held": "", "changed": False, "kept_lead": False, "remove_lists": []}


def decide(rec: dict, e: dict, s: dict, cfg, missing: set) -> dict:
    """What a live run writes on this record, every guard applied. Nothing is written here."""
    ow = rec.get("owner") or {}
    status = lanes.status_of(rec)
    have_tags = {t.lower() for t in titles(rec.get("tags"))}
    have_lists = {t.lower() for t in titles(rec.get("lists"))}
    working = {w.lower() for w in cfg.working_statuses}
    top = e["top"]
    plan = _blank_plan()
    plan["changed"] = changed = owner_changed(e["rows"], ow)
    hard = ""
    if top != "remove":
        hard = hard_hold(rec, cfg) or ("owned by a company, or by no one with a full name (people only)"
                                       if top in RECORD_CLASSES and is_company(ow) else "")
    marks = dict(e.get("phones") or {})
    if changed:
        marks = {n: k for n, k in marks.items() if k in ("dnc", "wrong", "dead")}     # about the number, not the owner
        plan["person"].append("the owner on this record has changed since the file was sent")
    if hard:
        marks = {n: k for n, k in marks.items() if k != "correct"}
    by_number = {gate.digits(p.get("number")): p for p in ow.get("phones") or [] if gate.digits(p.get("number"))}
    for n, kind in marks.items():
        p = by_number.get(n)
        if p is None:
            if kind != "conflict":
                plan["info"].append("a number the agency named is no longer on the record")
            continue
        cur = str(p.get("status") or "UNKNOWN").upper()
        new = ""
        if kind == "dnc" and cur not in DNC_FAMILY:
            new = DNC_FOR.get(cur, "DNC")
        elif kind == "correct":
            if cur in UNSET:
                new = "CORRECT"
            elif cur in ("WRONG", "DEAD"):
                plan["person"].append("the agency reached the owner on the number ending %s, which is marked %s" % (
                    ends(n), cur.lower()))
        elif kind in PHONE_CLASSES:
            if cur == "CORRECT":
                plan["person"].append("the agency called the number ending %s %s, but a person marked it correct" % (
                    ends(n), "a wrong number" if kind == "wrong" else "disconnected"))
            elif cur not in DNC_FAMILY | {"WRONG", "DEAD"}:
                new = "WRONG" if kind == "wrong" else "DEAD"
        if new:
            plan["phones"].append((p, new))
    dnm = (cfg.do_not_market_tags or ["Do Not Market"])[0]
    dead_tag = s["deceased_tag"]

    def want(names: list, have: set) -> list:
        return [t for t in dict.fromkeys(names) if t and t.lower() not in have and t.lower() not in missing]

    if top == "remove":
        if changed:
            plan["held"] = "the owner changed"
        else:
            plan["what"].append("removal")
            plan["tags"] = want([dnm], have_tags)
            plan["lists"] = want(list(s["remove_lists"]), have_lists)
            plan["remove_lists"] = [x for x in s["remove_lists"] if x.lower() not in missing]
            if s["all_houses"]:
                plan["house_tags"], plan["house_lists"] = want([dnm], set()), want(list(s["remove_lists"]), set())
    elif top in RECORD_CLASSES:
        if changed:
            plan["held"] = "the owner changed"
        elif e.get("hold"):
            plan["held"] = e["hold"]
        elif hard:
            plan["held"] = hard
            if top == "lead":
                plan["person"].append("the agency passed a lead, but the record is %s" % hard)
        elif top == "lead":
            if status in lanes.LEAD_STATUSES:
                plan["kept_lead"] = True
                plan["what"].append("lead kept")
            elif status in s["lead_over"] or status in working:
                plan["status"] = "new_lead"
                plan["what"].append("lead")
            else:
                # A closed or sold record is not reopened by a caller's yes: a person looks first.
                plan["held"] = "status %s" % status
                plan["person"].append("the agency passed a lead, but the record's status is %s, so New Lead was not set" % status)
            if not plan["held"]:
                plan["tags"] = want([s["lead_tag"]] + ([dead_tag] if e.get("deceased") else []), have_tags)
        elif top in ("not_interested", "listed"):
            if status in lanes.LEAD_STATUSES:
                plan["person"].append("the agency was told %s, but this record is a lead (%s), so the status stayed" % (
                    "not interested" if top == "not_interested" else "the house is listed", status))
            elif status != top and (status in s["ni_over"] or status in working):
                plan["status"] = top
                plan["what"].append(top)
        elif top == "deceased":
            plan["tags"] = want([dead_tag], have_tags)
            if s["all_houses"]:
                plan["house_tags"] = want([dead_tag], set())
            if plan["tags"]:
                plan["what"].append("deceased")
    if top == "remove" or (top in RECORD_CLASSES and not plan["held"]
                           and (plan["status"] or plan["tags"] or top == "lead" or plan["what"])):
        plan["note"] = note_for(e, plan, s, dnm)
    if e.get("noted"):
        plan["note"] = ""
    return plan


def writes_in(plan: dict) -> bool:
    return bool(plan["status"] or plan["tags"] or plan["lists"] or plan["phones"] or plan["note"]
                or plan["house_tags"] or plan["house_lists"])


def describe(plan: dict, houses: int = 0) -> list[str]:
    """What the plan writes, in short words for the CSV."""
    out = []
    if plan["status"]:
        out.append("status %s" % plan["status"])
    if plan["tags"]:
        out.append("tag %s" % ", ".join(plan["tags"]))
    if plan["lists"]:
        out.append("list %s" % ", ".join(plan["lists"]))
    for p, new in plan["phones"]:
        out.append("number ending %s to %s" % (ends(gate.digits(p.get("number"))), new))
    if houses and (plan["house_tags"] or plan["house_lists"]):
        out.append("%s of the owner's other houses too" % houses)
    if plan["note"]:
        out.append("a note")
    return out


# ---------------------------------------------------------------------- one record: the writes
def houses_of(crm_, owner_uuid: str, main: str, cap: int) -> list[str]:
    if not owner_uuid:
        return []
    rows = crm_.owner_records(owner_uuid, cap=cap) or []
    return [r["uuid"] for r in rows if r.get("uuid") and r["uuid"] != main][:cap]


def apply(crm_, u: str, rec: dict, plan: dict, run_id: str, e: dict, houses: list[str]) -> list[str]:
    """Write the plan. Each change is remembered with the value it replaced BEFORE it is made (a write that
    then fails leaves a change that undo simply re-applies)."""
    from ..autopilot.stages import phone_payload, phone_tier
    ow = rec.get("owner") or {}
    wrote = []
    if plan["phones"] and ow.get("uuid"):
        before = [phone_payload(p, phone_tier(p)) for p, _ in plan["phones"]]
        after = [dict(phone_payload(p, phone_tier(p)), status=new) for p, new in plan["phones"]]
        ledger.change(run_id, WORKFLOW, u, "phones", before, [x["number"] for x in after])
        crm_.upsert_phones(ow["uuid"], after)
        wrote.append("phones")
    if plan["status"]:
        ledger.change(run_id, WORKFLOW, u, "status", _raw_status(rec) or None, plan["status"])
        crm_.set_status(u, plan["status"])
        wrote.append("status")
    if plan["tags"]:
        ledger.change(run_id, WORKFLOW, u, "add_tags", [], list(plan["tags"]))
        crm_.add_tags(u, list(plan["tags"]), rec)
        wrote.append("tags")
    if plan["lists"]:
        have = titles(rec.get("lists"))
        ledger.change(run_id, WORKFLOW, u, "add_lists", have, have + list(plan["lists"]))
        crm_.add_lists(u, list(plan["lists"]))
        wrote.append("lists")
    for h in houses if (plan["house_tags"] or plan["house_lists"]) else []:
        hrec = crm_.record(h) or {}
        if not hrec.get("uuid"):
            continue
        ht = {t.lower() for t in titles(hrec.get("tags"))}
        hl = titles(hrec.get("lists"))
        add_t = [t for t in plan["house_tags"] if t.lower() not in ht]
        add_l = [t for t in plan["house_lists"] if t.lower() not in {x.lower() for x in hl}]
        if add_t:
            ledger.change(run_id, WORKFLOW, h, "add_tags", [], add_t)
            crm_.add_tags(h, add_t, hrec)
        if add_l:
            ledger.change(run_id, WORKFLOW, h, "add_lists", hl, hl + add_l)
            crm_.add_lists(h, add_l)
        if add_t or add_l:
            wrote.append("house")
    if plan["note"]:
        crm_.message(u, plan["note"])
        e["noted"] = True
        wrote.append("note")
    return wrote


def read_back(crm_, u: str, plan: dict) -> str:
    """'' when the first record of a run reads back as written, else what did not stick."""
    back = crm_.record(u) or {}
    if plan["status"] and lanes.status_of(back) != plan["status"].lower():
        return "the status"
    tags = {t.lower() for t in titles(back.get("tags"))}
    miss = [t for t in plan["tags"] if t.lower() not in tags]
    if miss:
        return "the tag %s" % miss[0]
    lists = {t.lower() for t in titles(back.get("lists"))}
    miss = [t for t in plan["lists"] if t.lower() not in lists]
    if miss:
        return "the list %s" % miss[0]
    shown = {gate.digits(p.get("number")): str(p.get("status") or "").upper()
             for p in (back.get("owner") or {}).get("phones") or []}
    for p, new in plan["phones"]:
        if shown.get(gate.digits(p.get("number"))) != new:
            return "the number's status"
    return ""


def order_of(ret: dict) -> list[str]:
    recs = ret.get("records") or {}
    return sorted(recs, key=lambda u: (ORDER.get(recs[u]["top"], 9), min(r["row"] for r in recs[u]["rows"])))


def missing_marks(crm_, s: dict, cfg) -> tuple[set, list[str]]:
    """(the tags and lists this run would write that the account does not have, the notes that says). A
    write never creates a tag: the note names it so a person creates it once, and until then it is left off."""
    dnm = (cfg.do_not_market_tags or ["Do Not Market"])[0]
    tags = [t for t in dict.fromkeys([dnm, s["lead_tag"], s["deceased_tag"]]) if t]
    missing, notes = set(), []
    for kind, names, look in (("tag", tags, crm_.tag_uuid), ("list", list(s["remove_lists"]), crm_.list_uuid)):
        for t in names:
            try:
                if not look(t):
                    missing.add(t.lower())
                    notes.append("the %s %s does not exist in this account yet: create it in DataSift and ask again; "
                                 "until then it is left off" % (kind, t))
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001  (an index that cannot be read is said; the write then tries)
                notes.append("the account's %ss could not be read, so whether the %s %s exists is not known" % (kind, kind, t))
    if dnm.lower() in missing and all(x.lower() in missing for x in s["remove_lists"]):
        notes.append("neither the tag %s nor a do-not-call list exists, so a removal reaches the gate only through the "
                     "number's do-not-call status" % dnm)
    return missing, notes


def process(crm_, ret: dict, s: dict, cfg, live: bool, now: datetime, tz: str, only_remove: bool,
            missing: set, problems: list) -> Counter:
    """Read and write (dry: read and decide) the next records of the file in order, removals first, at most
    max_records. Returns what happened, counted."""
    stats = Counter()
    todo = [u for u in order_of(ret) if ret["records"][u]["state"] == "pending"
            and (not only_remove or ret["records"][u]["top"] == "remove")]
    first = True
    for u in todo[: int(s["max_records"])]:
        e = ret["records"][u]
        stats["read"] += 1
        try:
            rec = crm_.record(u) or {}
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (a 404 is a record the account no longer holds; anything else waits)
            if getattr(exc, "code", None) != 404:
                stats["read failed"] += 1
                e["why"] = "could not be read (%s); tried again on the next run" % type(exc).__name__
                continue
            rec = {}
        if not rec.get("uuid"):
            e.update(state="unmatched", why="no record with this Record ID in the account (deleted or merged since the file was sent)")
            stats["unmatched"] += 1
            continue
        e["street"] = street_of(rec) or e.get("street") or ""
        e["sent"] = bool(ledger.fact(u, FACT_SENT))
        if e["key"] in ((ledger.fact(u, FACT_DONE) or {}).get("keys") or []):
            e.update(state="already", why="the same answer was written from an earlier file")
            stats["already"] += 1
            continue
        plan = decide(rec, e, s, cfg, missing)
        houses = []
        if plan["house_tags"] or plan["house_lists"]:
            houses = houses_of(crm_, (rec.get("owner") or {}).get("uuid") or "", u, int(s["max_houses"]))
        e["person"] = list(dict.fromkeys((e.get("person") or []) + plan["person"]))
        if not live:
            e["dry"] = {"would": describe(plan, len(houses)), "held": plan["held"]}
            stats["dry"] += 1
            for w in plan["what"]:
                stats["would " + w] += 1
            stats["would phones"] += len(plan["phones"])
            continue
        if not writes_in(plan):
            held = plan["held"] or "; ".join(plan["person"])
            e.update(state="held" if held else "nothing", why=held or "nothing left to write")
            stats[e["state"]] += 1
            continue
        try:
            wrote = apply(crm_, u, rec, plan, ret["run_id"], e, houses)
        except budget.BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001  (one record failing must not stop the rest)
            e.update(state="error", why="a write failed (%s)" % type(exc).__name__)
            stats["error"] += 1
            continue
        e.update(state="written", wrote=describe(plan, len(houses)), why="; ".join(plan["person"]),
                 written=now.astimezone(ZoneInfo(tz)).date().isoformat())
        done = ledger.fact(u, FACT_DONE) or {}
        ledger.set_fact(u, FACT_DONE, {"keys": list(done.get("keys") or []) + [e["key"]], "return": ret["id"]})
        ledger.event(ret["run_id"], WORKFLOW, u, "agency_result", {"return": ret["id"], "top": e["top"], "wrote": wrote},
                     now=now, tz=tz)
        stats["written"] += 1
        for w in plan["what"]:
            stats[w] += 1
        stats["phones"] += len(plan["phones"])
        if first:
            first = False
            bad = read_back(crm_, u, plan)
            if bad:
                ret["stopped"] = ("%s did not read back on %s, so nothing more was written; check the account, then "
                                  "ask again" % (bad, e["street"] or u[:8]))
                break
    return stats


# ---------------------------------------------------------------------- the PDF (the agency's own summary)
def pdf_pages(path: Path) -> list[str]:
    if PDF_TEXT is not None:
        return list(PDF_TEXT(path))
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range())
            tp.close()
            page.close()
        return out
    finally:
        doc.close()


NUM_RX = re.compile(r"(\$\s?)?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(%)?")
LABEL_RX = re.compile(r"\b(dials?|calls?|connect\w*|contacts?|conversations?|leads?|appointments?|appts?|transfers?|"
                      r"hours?|days?|talk|wrong|dnc|interested|spend|spent|cost|price|invoice|voicemails?|answer\w*|"
                      r"rate|worked|offers?|minutes?)\b", re.I)


def show(x: dict) -> str:
    """One of the agency's numbers, cleaned and rounded the way a person reads it (Ty: their numbers rounded and
    cleaned, not re-derived): $3,995, 1,200, 75.4%."""
    v = float(x.get("value") or 0)
    if x.get("kind") == "money":
        return "$%s" % (format(v, ",.0f") if v == int(v) else format(v, ",.2f"))
    if x.get("kind") == "percent":
        return "%s%%" % format(round(v, 1), "g")
    return format(int(v), ",") if v == int(v) else format(round(v, 2), ",")


def month_label(label: str) -> str:
    try:
        return date.fromisoformat(str(label) + "-01").strftime("%B %Y")
    except ValueError:
        return str(label or "")


def _value(m) -> tuple[float, str]:
    """(value, kind) of one printed number: a count, money or a percent. "$3.995" is read as 3,995, the way a
    European separator prints it: a dollar amount never carries three decimal places."""
    money, whole, frac, pct = m.group(1), m.group(2), m.group(3), m.group(4)
    v = float(whole.replace(",", ""))
    if frac:
        if money and len(frac) == 3 and "," not in whole:
            v = v * 1000 + float(frac)
        else:
            v += float("0." + frac)
    return v, ("money" if money else "percent" if pct else "count")


def pdf_numbers(pages: list[str]) -> list[dict]:
    """The label and number pairs a summary page prints, in order. An infographic's text often comes out of
    order, so a number on a line of its own is paired with the label on the next line, else the line before."""
    lines = [" ".join(ln.split()) for p in pages for ln in (p or "").splitlines()]
    lines = [ln for ln in lines if ln]
    out, seen = [], set()

    def add(label: str, m) -> None:
        lab = clean(re.sub(r"[:|]+", " ", label), 40).strip(" -,.")
        if not lab or not LABEL_RX.search(lab):
            return
        v, kind = _value(m)
        key = (lab.lower(), round(v, 4))
        if key in seen:
            return
        seen.add(key)
        out.append({"label": lab, "value": v, "kind": kind, "text": m.group(0).strip()})

    for i, ln in enumerate(lines):
        nums = list(NUM_RX.finditer(ln))
        if len(nums) != 1:
            continue
        rest = NUM_RX.sub(" ", ln).strip()
        if rest and re.search(r"[A-Za-z]{3,}", rest):
            add(rest, nums[0])
            continue
        if not re.search(r"[A-Za-z]", rest):
            for j in (i + 1, i - 1):
                if 0 <= j < len(lines) and not NUM_RX.search(lines[j]) and LABEL_RX.search(lines[j]):
                    add(lines[j], nums[0])
                    break
    return out


def ratio_notes(nums: list[dict]) -> list[str]:
    """A ratio the agency prints under a label like "dials to connect" is checked against its own counts: one
    such label on a real report was dials per lead, proven by dividing."""
    def count(word: str):
        for x in nums:
            lab = x["label"].lower()
            if x["kind"] == "count" and word in lab and " to " not in lab and " per " not in lab and "rate" not in lab:
                return x["value"]
        return None
    dials, leads, connects = count("dial"), count("lead"), count("connect")
    pairs = [("dials per lead", dials, leads), ("connects per lead", connects, leads), ("dials per connect", dials, connects)]
    out = []
    for x in nums:
        lab = x["label"].lower()
        if " to " not in lab and " per " not in lab:
            continue
        for name, a, b in pairs:
            if a and b and x["value"] and abs(x["value"] - a / b) <= 0.03 * x["value"]:
                out.append("Its %s (%s) is %s: %s over %s." % (x["label"], show(x), name, format(int(a), ","),
                                                            format(int(b), ",")))
                break
    return out


def _pdf_run(task: dict, pdfs: list[dict], month_in: str, s: dict, now: datetime, tz: str, folder: Path,
             out: dict) -> dict:
    """Only the summary came back: read its numbers for the scorecard, write nothing, and say what to ask for."""
    pages, failed = [], []
    for f in pdfs:
        try:
            pages += pdf_pages(Path(f["path"]))
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": f["name"], "why": "it could not be opened (%s)" % type(exc).__name__})
    if not pages:
        out["say"] = "The PDF could not be read. %s" % " ".join("%s: %s." % (x["name"], x["why"]) for x in failed[:2])
        return out
    nums = pdf_numbers(pages)
    month, how = month_of(month_in, [], [f["name"] for f in pdfs] + pages, now, tz)
    text = "\n\n".join((p or "").replace(EM_DASH, "-").replace(EN_DASH, "-") for p in pages)
    (folder / TEXT_PDF).write_text(text or "(no text)", encoding="utf-8")
    rid = return_id("pdf", [month] + pages)
    ret = load_return(rid) or {"id": rid, "kind": "pdf", "received": now.isoformat(timespec="seconds"),
                               "day": now.astimezone(ZoneInfo(tz)).date().isoformat(), "task": int(task.get("id") or 0),
                               "files": [f["name"] for f in pdfs]}
    ret.update(month=month, month_from=how, numbers=nums, notes=ratio_notes(nums),
               connect=any("connect" in x["label"].lower() for x in nums))
    save_return(ret)
    out.update(ok=True, kind="pdf", rid=rid, month=month, month_from=how, numbers=nums, notes=ret["notes"],
               connect=ret["connect"], files=[TEXT_PDF], skipped=failed, ask=s["ask"])
    return out


# ---------------------------------------------------------------------- the run
def _enqueue(task: dict, ret: dict, delay_s: float) -> int:
    enq = ENQUEUE or store.enqueue
    tid, _ = enq(task.get("agent") or "ops", WORKFLOW, "", subject="agency results, continued",
                 payload={"continue": ret["id"], "requested_by": "agency results"},
                 thread_id=task.get("thread_id") or "", parent_id=int(task.get("id") or 0), hop=int(task.get("hop") or 0),
                 source="workflow:%s" % WORKFLOW, mode="direct", slack_channel=task.get("slack_channel") or "",
                 slack_thread=task.get("slack_thread") or "", not_before=store.later(delay_s))
    return int(tid or 0)


def _pending(ret: dict, only_remove: bool) -> int:
    return sum(1 for e in (ret.get("records") or {}).values()
               if e["state"] == "pending" and (not only_remove or e["top"] == "remove"))


def _needs_go(ret: dict) -> bool:
    """A large file waits for a person's go, decided once when it is first read: as its removals are written
    the count falls, and a file must not slip under the line on its own."""
    return bool(ret.get("needs_go")) and not ret.get("go")


def _chunk(task: dict, ret: dict, s: dict, cfg, live: bool, now: datetime, tz: str, out: dict) -> Counter:
    """One run's worth of reading and writing, then the next run queued when there is more and the box is live."""
    try:
        crm_ = crm()
    except qs.MissingKey as exc:
        out["say"] = "%s; nothing was read." % qs.cap1(str(exc))
        return Counter()
    try:
        missing, notes = missing_marks(crm_, s, cfg)
    except budget.BudgetExhausted as exc:
        out["problems"].append("stopped for now: %s; the rest continues in %d minutes" % (exc, int(s["wait_s"]) // 60))
        missing, notes = set(), []
        stats = Counter({"budget": 1})
    else:
        stats = None
    out["notes"] = notes + list(s["code_problems"])
    only_remove = live and _needs_go(ret)
    if stats is None:
        try:
            stats = process(crm_, ret, s, cfg, live, now, tz, only_remove, missing, out["problems"])
        except budget.BudgetExhausted as exc:
            stats = Counter({"budget": 1})
            out["problems"].append("stopped for now: %s; the rest continues in %d minutes" % (exc, int(s["wait_s"]) // 60))
    if live:
        ret["runs"] = int(ret.get("runs") or 0) + 1
        ret["runs_total"] = int(ret.get("runs_total") or 0) + 1
        ret["written"] = sum(1 for e in ret["records"].values() if e["state"] == "written")
        left = _pending(ret, only_remove)
        if ret.get("stopped"):
            ret["chain"] = 0
        elif left and ret["runs"] >= int(s["max_runs"]):
            ret["stopped"] = "the file took %d runs and %d records are still waiting; ask again to go on" % (ret["runs"], left)
            ret["chain"] = 0
        elif left:
            queued = store.task(int(ret.get("chain") or 0)) if ret.get("chain") else None
            if not (queued and queued.get("status") == "queued"):
                ret["chain"] = _enqueue(task, ret, s["wait_s"] if stats.get("budget") else s["gap_s"])
        else:
            ret["chain"] = 0
            ret["done"] = not _pending(ret, False)
            if ret["done"]:
                ret["finished"] = now.isoformat(timespec="seconds")
    save_return(ret)
    return stats


def write_csvs(folder: Path, ret: dict, live: bool) -> list[str]:
    """Every record and what happened to it, and the rows a person must look at. A number is its last four."""
    made = []
    with open(folder / CSV_ALL, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Record ID", "Street", "The agency said", "Outcome", "Written" if live else "A live run writes",
                    "Why", "Sent to the agency"])
        for u in order_of(ret):
            e = ret["records"][u]
            said = "; ".join(dict.fromkeys(r["disposition"] or "(blank)" for r in e["rows"]))
            what = e.get("wrote") if (live or e["state"] != "pending") else ((e.get("dry") or {}).get("would") or [])
            state = e["state"] if live or e["state"] != "pending" else ("read" if e.get("dry") is not None else "not read yet")
            why = e.get("why") or (e.get("dry") or {}).get("held") or "; ".join(e.get("person") or [])
            w.writerow([u, clean(e.get("street"), 100), clean(said, 200), state.replace("_", " "),
                        clean("; ".join(what or []), 300), clean(why, 300),
                        {True: "yes", False: "not known"}.get(e.get("sent"), "")])
    made.append(CSV_ALL)
    rows = list(ret.get("unmatched") or [])
    for u in order_of(ret):
        e = ret["records"][u]
        # A record held only because the gate already holds it (a no on a Do Not Market record) needs nobody:
        # held records come in through their own reasons for a person.
        reasons = list(e.get("person") or [])
        if e["state"] in ("unmatched", "error") and e.get("why"):
            reasons = [e["why"]] + reasons
        for why in dict.fromkeys(x for x in reasons if x):
            rows.append({"row": e["rows"][0]["row"], "file": e["rows"][0]["file"], "record": u,
                         "street": e.get("street") or "",
                         "disposition": "; ".join(dict.fromkeys(r["disposition"] for r in e["rows"])),
                         "number": ", ".join(dict.fromkeys(ends(r["phone"]) for r in e["rows"] if r.get("phone"))),
                         "why": why})
    if rows:
        with open(folder / CSV_PERSON, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Row", "File", "Record ID", "Street", "Disposition", "Number ends", "Why"])
            for r in rows[:PERSON_ROWS]:
                w.writerow([r.get("row"), clean(r.get("file"), 80), r.get("record"), clean(r.get("street"), 100),
                            clean(r.get("disposition"), 120), r.get("number") or "", clean(r.get("why"), 300)])
        made.append(CSV_PERSON)
    return made


# Each reason a person must look, worded to follow a count ("2 with no usable Record ID").
REASON_KINDS = (("a disposition I do not know", "with a disposition I do not know"),
                ("the agency was told:", "told sold, a buyer and the like"),
                ("the owner on this record has changed", "whose owner changed"),
                ("the owner changed", "whose owner changed"),
                ("no record with this Record ID", "not in the account any more"),
                ("a write failed", "where a write failed"),
                ("the agency passed a lead, but the record is", "with a lead the gate holds"),
                ("the agency passed a lead, but the record's status", "with a lead on a finished record"),
                ("one call asked to be removed", "with a removal and a lead together"),
                ("the agency was told", "with a no or a listing on a lead"),
                ("the number ending", "where the calls disagree"),
                ("the agency reached the owner on the number", "where a person marked the number otherwise"),
                ("the agency called the number", "where a person marked the number otherwise"),
                ("the calls on this house disagree", "where the calls disagree"))


def _reason_kind(text: str) -> str:
    return next((kind for start, kind in REASON_KINDS if text.startswith(start)), "with another question")


def person_count(ret: dict) -> tuple[int, Counter]:
    """(how many rows and records need a person, by their first reason)."""
    reasons = Counter()
    for _ in ret.get("unmatched") or []:
        reasons["with no usable Record ID"] += 1
    for e in (ret.get("records") or {}).values():
        first = ""
        if e["state"] in ("unmatched", "error"):
            first = e.get("why") or ""
        elif e.get("person"):
            first = e["person"][0]
        if first:
            reasons[_reason_kind(first)] += 1
    return sum(reasons.values()), reasons


WRITTEN = ("written", "already")
STATE_RANK = {"written": 0, "already": 0, "held": 1, "unmatched": 1, "error": 1, "nothing": 2, "person": 2, "pending": 3}


def merge_returns(returns: list[dict]) -> dict:
    """Several files for one month (a corrected file posted again) read as one: each house once, as the newest
    file says it, with the furthest it ever got (written beats waiting). Without this a re-posted file counts
    every lead twice."""
    returns = sorted(returns, key=lambda r: str(r.get("received") or ""))
    merged: dict = {}
    for ret in returns:
        for u, e in (ret.get("records") or {}).items():
            old = merged.get(u)
            new = dict(e)
            if old and STATE_RANK.get(old["state"], 9) < STATE_RANK.get(new["state"], 9):
                new["state"] = old["state"]
            if new["state"] == "already":
                new["state"] = "written"
            merged[u] = new
    newest = returns[-1] if returns else {}
    counts = Counter(r["class"] for e in merged.values() for r in e["rows"])
    for r in newest.get("unmatched") or []:
        counts[classify(r.get("disposition"))] += 1
    counts.update(newest.get("quiet_bad") or {})
    rows = sum(len(e["rows"]) for e in merged.values()) + len(newest.get("unmatched") or []) \
        + sum((newest.get("quiet_bad") or {}).values())
    return {"records": merged, "row_counts": dict(counts), "rows": rows, "unmatched": list(newest.get("unmatched") or []),
            "quiet_bad": dict(newest.get("quiet_bad") or {})}


def summarize(ret: dict) -> dict:
    """The whole file's state, for the answer and the final post."""
    recs = ret.get("records") or {}
    order = sorted(recs, key=lambda u: min(r["row"] for r in recs[u]["rows"]))
    leads = [(recs[u].get("street") or recs[u]["rows"][0].get("address") or u[:8], recs[u]["state"])
             for u in order if recs[u]["top"] == "lead"]
    n_person, reasons = person_count(ret)
    return {"counts": dict(tally(ret)), "states": dict(Counter(e["state"] for e in recs.values())), "leads": leads,
            "person": n_person, "reasons": dict(reasons), "records": len(recs), "rows": ret.get("rows", 0),
            "to_write": sum(1 for e in recs.values() if e["top"] in RECORD_CLASSES or e["phones"]),
            "never_sent": sum(1 for e in recs.values() if e.get("sent") is False),
            "removals_written": sum(1 for e in recs.values() if e["top"] == "remove" and e["state"] == "written"),
            "removals_waiting": sum(1 for e in recs.values() if e["top"] == "remove" and e["state"] == "pending")}


def run(task: dict) -> dict:
    s = settings()
    inputs, _details = qs.inputs_of(task)
    payload = store.payload_of(task)
    pol = _policy()
    cfg, tz = (getattr(pol, "gate", None) or gate.GateConfig()), (getattr(pol, "tz", None) or TZ)
    live = config.live()
    now = _now()
    folder = qs.files_dir(task, WORKFLOW)
    out = {"ok": False, "live": live, "run_dir": str(folder.parent), "files": [], "problems": [], "notes": [],
           "kind": "records", "max_records": int(s["max_records"])}
    cont = str(payload.get("continue") or "")
    if cont:
        return _continue(task, cont, s, cfg, live, now, tz, folder, out)
    month_in = str(inputs.get("month") or "").strip()
    if month_in and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month_in):
        out["say"] = "The month has to be written like 2026-10; nothing was read."
        return out
    files, skipped = thread_files(task, folder)
    files, why = pick(files, inputs.get("files"))
    if why:
        out["say"] = "%s; nothing was read." % qs.cap1(why)
        return out
    tables = [f for f in files if f["kind"] == "table"]
    pdfs = [f for f in files if f["kind"] == "pdf"]
    if not tables and pdfs:
        return _pdf_run(task, pdfs, month_in, s, now, tz, folder, out)
    if not tables and skipped:
        out["say"] = "The agency's file is on this thread but could not be saved, so nothing was read. %s" % " ".join(
            "%s: %s." % (x["name"], x["why"]) for x in skipped[:2])
        return out
    if not tables:
        out["say"] = "No CSV, Excel or PDF from the agency is on this thread. Post their results file here and ask again."
        return out
    rows, refused = [], []
    for f in tables:
        got, why = parse_file(f, s["codes"])
        if why:
            refused.append("%s: %s" % (f["name"], why))
        rows += got
    if not rows:
        out["say"] = "Nothing could be read. %s. Ask the agency for %s." % ("; ".join(refused) or "The file is empty", s["ask"])
        return out
    if len(rows) > int(s["max_rows"]):
        out["say"] = "The file has %s rows, more than the %s one ask reads; post it in parts." % (
            format(len(rows), ","), format(int(s["max_rows"]), ","))
        return out
    month, how = month_of(month_in, [r["call_date"] for r in rows], [f["name"] for f in tables], now, tz)
    fresh = build_return(rows, [f["name"] for f in tables], month, how, task, now, s, tz)
    ret = load_return(fresh["id"])
    if ret and ret.get("done"):
        out.update(ok=True, rid=ret["id"], month=ret.get("month"), already=True, day=ret.get("day"),
                   summary=summarize(ret), files=write_csvs(folder, ret, live), refused=refused)
        return out
    ret = ret or fresh
    if month_in:
        ret.update(month=month, month_from=how)
    ret["go"] = bool(ret.get("go") or str(inputs.get("go") or "").strip().lower() in GO_WORDS)
    ret["stopped"], ret["runs"] = "", 0          # a person's ask starts the file's count of runs again
    stats = _chunk(task, ret, s, cfg, live, now, tz, out)
    if out.get("say"):
        return out
    out.update(ok=True, rid=ret["id"], month=ret.get("month"), month_from=ret.get("month_from"), stats=dict(stats),
               summary=summarize(ret), files=write_csvs(folder, ret, live), refused=refused, skipped=skipped,
               need_go=live and _needs_go(ret), pending=_pending(ret, live and _needs_go(ret)),
               all_pending=_pending(ret, False), done=bool(ret.get("done")), stopped=ret.get("stopped") or "",
               chain=int(ret.get("chain") or 0), ask=s["ask"])
    if live and ledger.changes(run_id=ret["run_id"]):
        out["run_id"] = ret["run_id"]
    return out


def _continue(task: dict, rid: str, s: dict, cfg, live: bool, now: datetime, tz: str, folder: Path, out: dict) -> dict:
    """A background run of a file already read: the next records, then the next run, and one post at the end
    (or when the removals are all written and the rest waits for a go)."""
    ret = load_return(rid)
    if not ret:
        out["say"] = "The agency file this run was continuing is no longer on the box, so nothing more was written."
        return out
    if int(ret.get("chain") or 0) != int(task.get("id") or 0):
        out.update(ok=True, quiet=True)          # a newer run took the file over
        return out
    ret["chain"] = 0
    if not live:
        save_return(ret)
        out.update(ok=True, rid=rid, say="The box is dry now, so the rest of the agency file waits; ask again once it is live.")
        return out
    stats = _chunk(task, ret, s, cfg, live, now, tz, out)
    if out.get("say"):
        return out
    paused = _needs_go(ret) and not _pending(ret, True)
    final = bool(ret.get("done") or ret.get("stopped") or paused)
    out.update(ok=True, rid=rid, month=ret.get("month"), stats=dict(stats), summary=summarize(ret), final=final,
               done=bool(ret.get("done")), stopped=ret.get("stopped") or "", paused=paused, quiet=not final,
               all_pending=_pending(ret, False), runs=int(ret.get("runs_total") or ret.get("runs") or 0), ask=s["ask"],
               continued=True)
    if final:
        out["files"] = write_csvs(folder, ret, live)
    if ledger.changes(run_id=ret["run_id"]):
        out["run_id"] = ret["run_id"]
    return out


# ---------------------------------------------------------------------- the answer in the thread
SAID = (("lead", "lead", "leads"), ("not_interested", "not interested", "not interested"),
        ("wrong", "wrong number", "wrong numbers"), ("dead", "disconnected number", "disconnected numbers"),
        ("remove", "asked to be removed", "asked to be removed"), ("deceased", "told the owner died", "told the owner died"),
        ("listed", "listed with an agent", "listed with an agent"),
        ("callback", "callback the agency keeps", "callbacks the agency keeps"),
        ("no_contact", "no answer or voicemail", "no answer or voicemail"))
WROTE = (("removal", "removal (Do Not Market, the DNC list and the number)", "removals (Do Not Market, the DNC list and the number)"),
         ("lead", "lead set to New Lead", "leads set to New Lead"), ("lead kept", "lead kept as it was", "leads kept as they were"),
         ("not_interested", "set to Not Interested", "set to Not Interested"), ("listed", "set to Listed", "set to Listed"),
         ("deceased", "tagged to verify a death", "tagged to verify a death"))


def said_line(c: dict) -> str:
    parts = ["%s %s" % (format(int(c[k]), ","), one if c[k] == 1 else many) for k, one, many in SAID if c.get(k)]
    return ", ".join(parts) or "nothing anyone can act on"


def wrote_line(st: dict, live: bool) -> str:
    pre = "" if live else "would "
    parts = []
    for k, one, many in WROTE:
        n = int(st.get(pre + k) or 0)
        if n:
            parts.append("%s %s" % (format(n, ","), one if n == 1 else many))
    n = int(st.get(pre + "phones") or 0)
    if n:
        parts.append(_n(n, "number marked", "numbers marked"))
    return ", ".join(parts)


def render(task: dict, r: dict) -> str:
    """The thread answer: streets and counts, never a phone number, an owner's name or the agency's notes."""
    if r.get("quiet"):
        return ""
    if not r.get("ok") or r.get("say"):
        return clean(r.get("say") or "Agency results: nothing was read.", 3000)
    lines = _pdf_lines(task, r) if r.get("kind") == "pdf" else _record_lines(task, r)
    return "\n".join(lines).replace(EM_DASH, "-").replace(EN_DASH, "-")


def _pdf_lines(task: dict, r: dict) -> list[str]:
    lines = ["*Agency report for %s: their summary PDF*" % month_label(r.get("month"))]
    nums = r.get("numbers") or []
    if nums:
        lines.append("As the agency reports it: %s." % "; ".join("%s %s" % (x["label"], show(x)) for x in nums[:8]))
        lines += r.get("notes") or []
        if r.get("connect"):
            lines.append("A connect count includes voicemail and any answered line; it is not live conversations.")
    else:
        lines.append("Its text could not be read in order (an infographic often reads that way); a person reads the "
                     "numbers off the page.")
    lines.append("Nothing was written: the PDF has no record ids, so nobody the agency was asked to remove can be held "
                 "by our gate. Ask the agency for %s." % r.get("ask"))
    lines.append(qs.file_line(task, "Its text", TEXT_PDF))
    return [clean(x, 1200) for x in lines]


def _record_lines(task: dict, r: dict) -> list[str]:
    sm = r.get("summary") or {}
    live = r.get("live")
    if r.get("already"):
        return ["*Agency results for %s*" % month_label(r.get("month")),
                "This file was already written on %s (%s); nothing was written twice." % (
                    r.get("day"), _n((sm.get("states") or {}).get("written", 0), "record")),
                qs.file_line(task, "Every record and what happened to it", CSV_ALL)]
    head = "*Agency results%s: %s on %s, calls in %s*  (%s)" % (
        " finished" if r.get("done") and r.get("continued") else "", _n(sm.get("rows", 0), "row"),
        _n(sm.get("records", 0), "record"), month_label(r.get("month")), "live" if live else "dry run, nothing written")
    lines = [head, "Back from the agency: %s." % said_line(sm.get("counts") or {})]
    st = r.get("stats") or {}
    if not live:
        lines.append("Read %s of the %s with something to write. On those, a live run would write: %s. A live run writes "
                     "all of them, %d a run, removals first." % (
                         format(int(st.get("dry") or 0), ","), _n(sm.get("to_write", 0), "record"),
                         wrote_line(st, False) or "nothing", int(r.get("max_records") or 0)))
    elif r.get("continued"):
        if r.get("done"):
            lines.append("Written: %s over %s." % (_n((sm.get("states") or {}).get("written", 0), "record"),
                                                     _n(r.get("runs", 0), "run")))
        elif r.get("paused"):
            lines.append("The %s are all written, so the gate holds those owners. Say go and I write the other %s." % (
                _n(sm.get("removals_written", 0), "removal"), _n(r.get("all_pending", 0), "record")))
    else:
        wrote = wrote_line(st, True)
        lines.append("Wrote %s now%s." % (_n(st.get("written", 0), "record"), (": " + wrote) if wrote else ""))
        if r.get("stopped"):
            pass
        elif r.get("need_go"):
            lines.append("This file writes %s. %s Say go and I write the rest, %d a run in the background." % (
                _n(sm.get("to_write", 0), "record"),
                ("The %s were written now, so the gate holds those owners from today." % _n(sm.get("removals_written", 0), "removal"))
                if sm.get("removals_written") else "It has no removals to write first.",
                int(r.get("max_records") or 0)))
            if sm.get("removals_waiting"):
                lines.append("Removals still to write, in the background first: %s." % format(int(sm["removals_waiting"]), ","))
        elif r.get("all_pending"):
            lines.append("The other %s are written in the background, %d a run, and I post here when the file is done." % (
                _n(r["all_pending"], "record"), int(r.get("max_records") or 0)))
    if r.get("stopped"):
        lines.append("Stopped: %s." % clean(r["stopped"], 300).rstrip("."))
    leads = [tuple(x) for x in sm.get("leads") or []]
    shown = [s_ for s_, state in leads if state in WRITTEN] if live else [s_ for s_, _ in leads]
    if shown:
        lines.append("%s: %s%s." % ("Leads now in DataSift" if live else "Leads", "; ".join(clean(x, 60) for x in shown[:10]),
                                    "; and %d more" % (len(shown) - 10) if len(shown) > 10 else ""))
    if live and len(shown) < len(leads):
        waiting = sum(1 for _, state in leads if state == "pending")
        held = len(leads) - len(shown) - waiting
        bits = ([_n(held, "lead") + " not written (for a person, in the file)"] if held else []) + \
               (["%s still to write" % _n(waiting, "lead")] if waiting else [])
        lines.append("%s." % qs.cap1("; ".join(bits)))
    if sm.get("person"):
        kinds = sorted((sm.get("reasons") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
        why = ", ".join("%d %s" % (n, k) for k, n in kinds[:4])
        more = sum(n for _, n in kinds[4:])
        lines.append("Needs a person: %d (%s%s). They are in the file." % (sm["person"], why,
                                                                         ", and %d more" % more if more else ""))
    if sm.get("never_sent"):
        lines.append("%s read %s never in a file the autopilot knows it sent; check the file is ours." % (
            _n(sm["never_sent"], "record"), "was" if sm["never_sent"] == 1 else "were"))
    lines += ["Not read: %s." % clean(x, 200) for x in (r.get("refused") or [])[:2]]
    lines += ["Not saved: %s, %s." % (clean(x["name"], 80), clean(x["why"], 160)) for x in (r.get("skipped") or [])[:2]]
    lines += ["%s." % qs.cap1(clean(p, 300).rstrip(".")) for p in list(r.get("problems") or [])[:3]]
    lines += ["To fix: %s." % clean(p, 300).rstrip(".") for p in list(r.get("notes") or [])[:3]]
    files = r.get("files") or []
    if CSV_ALL in files:
        lines.append(qs.file_line(task, "Every record and what happens to it", CSV_ALL))
    if CSV_PERSON in files:
        lines.append(qs.file_line(task, "For a person", CSV_PERSON))
    if r.get("run_id") and (r.get("done") or not r.get("all_pending")):
        lines.append("To put the file's writes back: autopilot undo %s (a note has no delete route, so the notes stay)."
                     % r["run_id"])
    return lines
