"""Which department a piece of scheduled work belongs to, and what kind of problem a problem is.

Ty, 2026-11-19: "segment the notifications that are coming in by channel (like a marketing, a sales,
and ops channel)", keep every employee in each, and consolidate. Measured the week before: 262 posts
in seven days, 158 of them in #issues, and most of those were not breakage at all:
  - one post per address for one failure (26 for a death check that could not write its notes);
  - the team's own to-dos (12 leads with no open task, dials outside calling hours);
  - settings nobody had made yet (a closer not named, a tag not created), said again every day.

So every stage has a department (sales, marketing, ops), and every problem a class:
  alert     a seller to call now: its own post in the sales channel
  finding   the team's work (a lead with no task, a dial outside hours): a line on the department's board
  setup     a setting a person has to make (create a tag, name the closer): the ops board's to-do list,
            kept day to day until the stage stops asking for it, never said again as a new post
  breakage  our own code or a source failing: #issues, once a day per CAUSE, however many records it hit

Pure: no Slack, no store. The board (agents/board.py) and the sweep (autopilot/sweep.py) use it.
"""
from __future__ import annotations

import re

DEPARTMENTS = ("sales", "marketing", "ops")

# Every stage kind in team/autopilot.yaml, by the department that works from it. A stage can name its
# own (`slack: marketing` on its block); a kind not listed here is ops.
KIND_DEPARTMENT = {
    # sales: a lead, a caller, an appointment, a deal
    "leadclock": "sales", "callbacks": "sales", "appointments": "sales", "leadcheck": "sales",
    "leadtasks": "sales", "leadnotes": "sales", "leadprep": "sales", "encumbrance": "sales",
    "closertext": "sales", "attemptlog": "sales", "primaryphone": "sales", "fullattempt": "sales",
    "assign": "sales", "auction": "sales", "auctiontasks": "sales", "callscreen": "sales",
    "coachpick": "sales", "newhire": "sales", "audit": "sales", "limits": "sales", "exits": "sales",
    "drips": "sales", "secondchance": "sales", "reactivate": "sales", "dealsource": "sales",
    "lostdeals": "sales", "capture": "sales", "goals": "sales", "alarms": "sales", "callerbook": "sales",
    "kpitracker": "sales",
    # marketing: texts, mail, the agency, which list an owner is on
    "textlane": "marketing", "scrub": "marketing", "mail": "marketing", "mailcadence": "marketing",
    "agencyexport": "marketing", "agencyscore": "marketing", "dialexport": "marketing",
    "obituary": "marketing", "heirlane": "marketing", "top1000": "marketing", "suppression": "marketing",
    "cadence": "marketing", "contactrate": "marketing", "revoke": "marketing", "lanes": "marketing",
    "ftmpipeline": "marketing",
    "coldreplies": "marketing", "coldtopup": "marketing", "coldhealth": "marketing", "coldstats": "marketing",
    # ops: county data, research, scoring, the account's own upkeep
    "screen": "ops", "score": "ops", "native_trace": "ops", "retrace": "ops", "handoff": "ops",
    "researchqueue": "ops", "researchstats": "ops", "complete": "ops", "phonecleanup": "ops",
    "paidlookup": "ops", "taxwatch": "ops", "taxsale": "ops", "probate": "ops", "attempts": "ops",
    "watchdog": "ops", "accountchecks": "ops", "conventions": "ops", "laddercheck": "ops",
    "bizaudit": "ops", "digest": "ops",
    "skipscreen": "ops", "ownerswap": "ops", "curative": "sales",
}
# A workflow a stage queued (a deep prospect, comps for an appointment) reports where the stage that
# queued it does; one asked for with no stage behind it, by what it is.
WORKFLOW_DEPARTMENT = {
    "deep_prospect": "ops", "entity_principals": "ops", "probate_property": "ops", "skip_trace": "ops",
    "phone_score": "ops", "buyer_registry": "ops",
    "quick_screen": "sales", "underwrite": "sales", "offer_sheet": "sales", "deal_buyers": "sales",
    "call_coaching": "sales", "book_appointment": "sales", "walkthrough_video": "sales",
    "outreach_health": "marketing", "qa_batch": "marketing", "agency_results": "marketing",
    "returned_mail": "marketing",
}

# Kinds whose whole job is to find something for a person to do. Their problems are the team's work,
# unless the check itself broke ("could not read", "crashed").
FINDER_KINDS = {
    "leadcheck", "leadtasks", "accountchecks", "audit", "limits", "appointments", "callbacks", "callscreen",
    "newhire", "coachpick", "phonecleanup", "exits", "drips", "secondchance", "dealsource", "alarms",
    "goals", "conventions", "laddercheck", "bizaudit", "lostdeals", "watchdog", "researchstats", "cadence",
    "contactrate", "agencyscore", "suppression", "digest", "lanes",
}
# Kinds whose problems can be a seller to call now (speed to lead, the lead clock).
CALL_NOW_KINDS = {"leadclock"}


def department(kind: str = "", stage_id: str = "", block=None) -> str:
    """The department a stage reports to: its own `slack:` setting, else its kind's, else ops."""
    own = ""
    if block is not None:
        try:
            own = str((block.get("slack") if hasattr(block, "get") else getattr(block, "slack", "")) or "")
        except Exception:  # noqa: BLE001
            own = ""
    own = own.strip().lower()
    if own in DEPARTMENTS:
        return own
    return KIND_DEPARTMENT.get(kind or "", "ops")


def task_department(task: dict, kind_of_stage=None) -> str:
    """A task's department: a stage's own run by its kind; a workflow a stage queued, by that stage;
    anything else by the workflow. `kind_of_stage(stage_id) -> (kind, block)` reads the playbook."""
    source = str(task.get("source") or "")
    stage = source.split(":", 1)[1] if source.startswith("autopilot:") else ""
    if stage and kind_of_stage is not None:
        try:
            kind, block = kind_of_stage(stage)
            if kind:
                return department(kind, stage, block)
        except Exception:  # noqa: BLE001
            pass
    return WORKFLOW_DEPARTMENT.get(str(task.get("workflow") or ""), "ops")


# ---------------------------------------------------------------------- what a problem is
_SETUP = [re.compile(p, re.I) for p in (
    r"\bno (?:task )?(?:preset|tag|list|status|column|board|custom field|field|template|channel|webhook)s? "
    r"(?:titled|named|called)\b",
    r"\bthere is no [\"'“‘]?[^\"'”’]{2,60}[\"'”’]? "
    r"(?:tag|preset|list|status|column|board|field|custom field|template|channel)\b",
    r"\b(?:does|do) not exist (?:yet|in (?:this|the) account)\b",
    r"\bnot in DataSift yet\b",
    r"\b(?:is|are) not named\b|\bnobody is named\b|\bname it under\b",
    r"\bset `?\w+`? on the stage\b|\bin the stage block\b|\bset `\w+`",
    r"\bfilters on a status this account does not use\b",
    r"\bcreate (?:it|them)\b|\bcreate the (?:tag|preset|list|field|custom field|column)\b",
    r"\bhas no column named\b|\bno such column\b",
    r"\bno (?:Slack )?releasers?\b|\bAGENTS_RELEASERS\b",
    r"\bno \w+ design yet\b|\b(?:has|have) not been (?:made|created|set up)\b",
)]
# Our own code, or a source we read, failing. A finder's "10 of 10 texts failed" is about the account,
# not about us, so a bare "failed" counts here only for a stage that is not a finder.
_OURS = re.compile(
    r"\bcould ?n[o']t\b|\bcannot\b|\bcan't\b|\bcrash(?:ed|es)?\b|\bstopped before it finished\b|\bunreachable\b|"
    r"\btimed out\b|\btime-?out\b|\bHTTP \d{3}\b|\berror\b|\bexception\b|\bcheck failed\b|\bfailed to\b|"
    r"\b(?:stage|check|run|pull|load|lookup|read|write|update|post|request|upload) failed\b|"
    r"\bdid not (?:move|land|finish|load|stick|answer)\b|\bstill catching up\b|\bnot readable\b|\bunreadable\b|"
    r"\bwas refused\b|\bmust be (?:a|an|true|false|one of)\b", re.I)
_FAILED = re.compile(r"\bfailed\b", re.I)
# A record a person has to look at is the team's work even when it reads like a failure.
_PERSON = re.compile(r"\bcheck (?:it |them )?by hand\b|\bneeds? a person\b|\bfor a person\b", re.I)
_CALL_NOW = re.compile(r"\bneeds a call now\b|\bCall now\b|\bcall (?:them|it) now\b", re.I)


def classify(problem: str, kind: str = "") -> str:
    """alert, setup, finding or breakage (see the module's docstring)."""
    t = str(problem or "")
    if kind in CALL_NOW_KINDS and _CALL_NOW.search(t):
        return "alert"
    if any(r.search(t) for r in _SETUP):
        return "setup"
    if _PERSON.search(t) and not re.search(r"\bcould ?n[o']t\b|\bcrash", t, re.I):
        return "finding"
    if _OURS.search(t):
        return "breakage"
    return "finding" if kind in FINDER_KINDS else "breakage"


# ---------------------------------------------------------------------- one cause, many records
_SUFFIX = (r"St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Ct|Court|Cir|Circle|Blvd|Boulevard|Way|Pl|Place|Pike|"
           r"Hwy|Highway|Trl|Trail|Pkwy|Parkway|Ter|Terrace|Loop|Run|Rdg|Ridge|Xing|Crossing|Pt|Point|Sq|Square|Row|"
           r"Path|Walk|Cv|Cove|Hl|Hill|Holw|Hollow|Bnd|Bend|Vw|View|Trce|Trace|Pass|Grv|Grove|Hts|Heights|Spgs|"
           r"Springs|Mnr|Manor|Vly|Valley|Crk|Creek|Mtn|Mountain|Gln|Glen|Cres|Crescent|Aly|Alley|Plz|Plaza")
# A street line, its suffix, and the city after a comma (Title Case only: "Knoxville could not" is not a city).
ADDRESS = re.compile(
    r"\b\d{1,6}[A-Za-z]?\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?(?:[A-Za-z0-9][\w'.-]*\s+){0,4}"
    r"(?:%s)\b\.?(?:\s+(?:N|S|E|W|NE|NW|SE|SW)\b)?(?:\s+(?:Apt|Unit|Ste|#)\s*\w+)?"
    r"(?:,\s*(?-i:[A-Z][a-zA-Z.']+(?:\s+[A-Z][a-zA-Z.']+){0,2}))?" % _SUFFIX, re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
# The programmer's detail in parentheses ("(HTTP 404 on GET /api/...)"): different on every record,
# never part of the cause.
_TECH = re.compile(r"\s*\((?:[^()]*\b(?:HTTP|GET|POST|PATCH|PUT|DELETE|Error|Exception|Traceback|timed out|"
                   r"Max retries|Connection|port=|host=)\b[^()]*|[^()]*/api/[^()]*)\)", re.I)
_EXAMPLES = re.compile(r"\s*\((?:for example|e\.g\.|such as)\b[^()]*\)|\s*\bfor example\b[^.;]*", re.I)
_NUMBER = re.compile(r"\d[\d,.:/]*")
RECORD = "{record}"


def cause(problem: str) -> tuple[str, str, str]:
    """(key, template, record) for one problem: the same cause on different records has one key. The
    template holds {record} where the record was; record is the street it named, or ""."""
    t = _TECH.sub("", str(problem or ""))
    t = _EXAMPLES.sub("", t)
    rec = ""
    m = ADDRESS.search(t)
    if m:
        rec = m.group(0).strip(" ,.")
        t = t[:m.start()] + RECORD + t[m.end():]
    t = _UUID.sub(RECORD, t)
    t = re.sub(r"\s+", " ", t).strip(" ;")
    key = re.sub(r"\s+", " ", _NUMBER.sub("#", t)).strip(" .;:").lower()
    return key, t, rec


def group(problems: list) -> list[dict]:
    """The run's problems with one entry per cause, in first-seen order:
    [{"key", "text", "count", "records": [streets], "first": the first problem as written}]. One problem
    on 26 records reads "... on 26 records (for example 100 Example St, ...)"."""
    out: dict = {}
    for p in problems or []:
        key, template, rec = cause(p)
        g = out.setdefault(key, {"key": key, "template": template, "count": 0, "records": [], "first": str(p)})
        g["count"] += 1
        if rec and rec not in g["records"]:
            g["records"].append(rec)
    for g in out.values():
        g["text"] = say_group(g)
    return list(out.values())


def say_group(g: dict, examples: int = 3) -> str:
    """One cause in words: the problem as written when it hit one record, else counted."""
    recs = g.get("records") or []
    if g.get("count", 1) <= 1 or len(recs) <= 1:
        # as written, examples and all, without the programmer's detail in parentheses
        return re.sub(r"\s+", " ", _TECH.sub("", str(g.get("first") or g.get("template") or ""))).strip()
    n = len(recs)
    t = str(g.get("template") or "")
    noun = "%d records" % n
    text = t.replace(RECORD, noun, 1).replace(RECORD, "them") if RECORD in t else "%s (%s)" % (t, noun)
    shown = recs[:examples]
    more = n - len(shown)
    tail = "; ".join(shown) + ("; and %d more" % more if more > 0 else "")
    return "%s (for example %s)" % (text.rstrip(". "), tail)
