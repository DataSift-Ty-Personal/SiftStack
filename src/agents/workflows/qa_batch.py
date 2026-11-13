"""qa_batch: a staged batch checked before a person releases it. It never releases anything.

A batch (ap_batches) is work a stage worked out and a person must approve: owner texts, mail
pieces, heir letters, the attempt keeper's first correction, card moves. The release re-runs the
gate on every item, so nothing wrong goes out; what the release cannot do is tell the person
BEFORE they say yes that the batch they are looking at has problems. That is this workflow:

  * READ BACK THE FIRST RECORD. The uploader's habit (one record read back before a file is
    released) caught the tag format, the entity owners and a list that would have attached to
    nothing. Here: the first item's record is read again and compared with what the batch holds,
    its street, its owner's name and its numbers.
  * JUNK NAMES. A first name Smarter Contact prints raw ("E", "SMITH JOHN", "Payable To The Order
    Of") or an envelope with notice wording on it reads as a list, not a person.
  * NUMBERS THE GATE REFUSES, read off the record as it is now: marked wrong, dead or do-not-call,
    a landline on a text, a number no longer on the owner. And the gate's verdict on the record
    itself (a lead, an opt-out, a death, a sale since the batch was staged).
  * DUPLICATES: the same record, owner or number twice in the batch, or already waiting in
    another staged batch.
  * THE REGISTRY FLAG: a number the do-not-call registry flagged is never texted (decision D32).
  * THE HOUSE LIMITS (limits.room): one text per owner a day, five a week, the platform's rest,
    both texting systems counted; and a text batch is released on its day or not at all.

Reads are bounded (AGENTS_QA_MAX_READS records a run, the first item always): the release reads
every record again anyway, so a large batch is sampled and says how much of it was opened. On its
schedule the workflow checks only batches that are new or changed since it last looked, and says
nothing when there is nothing new. The dispo blast is staged in the texting app's own outbox, not
here, so this cannot see it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, slack, store
# Imported here, not inside a function, so the API audit (tools/datasift_api_audit.py) grades the
# route this workflow really reads by: GET /api/internal/property/{uuid}/ on the Open API key.
from ..autopilot import crm as crm_mod

log = logging.getLogger(__name__)

WORKFLOW = "qa_batch"
TZ = "America/New_York"
TEXT_KINDS = ("text", "heir_text")
MAIL_KINDS = ("mail", "heir_mail", "obituary_mail")
NOT_TOUCHES = ("attempts", "leadclock", "probate")
PLACEHOLDERS = {"unknown", "owner", "current", "resident", "occupant", "current resident", "current owner", "n/a",
                "na", "none", "null", "test", "homeowner", "tbd", "estate"}
# Words a notice or a court filing puts where a name belongs (seen live: "By The Chancery Court",
# "Payable To The Order Of <lender>"). Wider for a greeting than for an envelope, where "Jane Doe,
# Personal Representative" is a correct way to address a letter.
GREET_PROSE = re.compile(r"\b(court|clerk|chancery|payable|order of|trustee|successor|substitute|deed|notice|"
                         r"representative|executor|executrix|administrat\w*|estate of|by the|attorney|law (?:firm|group|office))\b", re.I)
ENVELOPE_PROSE = re.compile(r"\b(court|clerk|chancery|payable|order of|deed of trust|notice|by the|law (?:firm|group|office))\b", re.I)
NOT_MOBILE = {"landline", "fixedvoip", "fixed voip", "tollfree", "toll free", "premium", "voicemail"}

CRM = None          # stand-ins the selftest sets; None means the brain's own
POLICY = None
NOW = None


def _now() -> datetime:
    return NOW or datetime.now(timezone.utc)


def _policy():
    if POLICY is not None:
        return POLICY
    try:
        from ..autopilot import policy
        return policy.load()
    except Exception:  # noqa: BLE001
        return None


def _crm():
    """The brain's budgeted client on the Open API key: every read counts against the account's
    shared request limit like the stages' reads do."""
    return CRM if CRM is not None else crm_mod.CRM()


def max_reads() -> int:
    try:
        return max(1, int(float(os.environ.get("AGENTS_QA_MAX_READS") or 60)))
    except ValueError:
        return 60


# ---------------------------------------------------------------------- what an item is
def channel_of(kind: str, it: dict) -> str:
    if kind in TEXT_KINDS:
        return "text"
    if kind in MAIL_KINDS:
        return "mail"
    if kind == "drip":
        return "text" if it.get("do") == "text" else "task"
    return ""


def numbers_of(kind: str, it: dict) -> list[str]:
    from ..autopilot import gate
    if kind == "text":
        raw = [n.get("number") for n in it.get("numbers") or [] if isinstance(n, dict)]
    elif kind == "heir_text":
        raw = [it.get("number")]
    elif kind == "drip" and it.get("do") == "text":
        raw = [it.get("to")]
    else:
        raw = []
    return [d for d in (gate.digits(n) for n in raw) if d]


def street_of(it: dict) -> str:
    s = it.get("property_street") or it.get("street") or ""
    return " ".join(str(s).split())


def _entity(name: str) -> bool:
    try:
        from sms_agent.knowledge import touches
        return touches.is_entity(name)
    except Exception:  # noqa: BLE001
        return bool(re.search(r"\b(llc|inc|corp|trust|estate|bank|church|properties|holdings)\b", name or "", re.I))


def junk_greeting(first: str, last: str = "", raw_printed: bool = False) -> str:
    """Why this first name must not greet anyone, or "". `raw_printed`: the texting platform prints
    the field exactly as stored (Smarter Contact cannot clean it), so capitals and extra words count."""
    fn = " ".join(str(first or "").split())
    if not fn:
        return ""
    low = fn.lower().strip(".")
    if any(ch.isdigit() for ch in fn):
        return "digits in the first name"
    if low in PLACEHOLDERS:
        return "a placeholder, not a name"
    if GREET_PROSE.search(fn) or GREET_PROSE.search(str(last or "")):
        return "notice or court wording in the name"
    if _entity("%s %s" % (fn, last or "")):
        return "a company or trust greeted by first name"
    if len(fn.replace(".", "").replace(" ", "")) < 2:
        return "an initial, not a first name"
    if raw_printed and " " in fn:
        return "several words in the first-name field"
    if raw_printed and fn.isupper() and len(fn) > 1:
        return "a first name in all capitals"
    if last and low == str(last).strip().lower():
        return "the first and last name are the same word"
    return ""


def junk_envelope(name: str) -> str:
    n = " ".join(str(name or "").split())
    if not n:
        return "no name on the envelope"
    if any(ch.isdigit() for ch in n):
        return "digits in the name"
    if n.lower().strip(".") in PLACEHOLDERS:
        return "a placeholder, not a name"
    if ENVELOPE_PROSE.search(n):
        return "notice or court wording in the name"
    if len(n) > 60 or len(n.split()) > 7:
        return "reads like notice text, not a name"
    return ""


def names_of(kind: str, it: dict) -> list[str]:
    """Why any name this item puts in front of a person is junk (empty list when none is)."""
    out = []
    if kind == "text":
        named = (it.get("audience") or "named") == "named"
        if named and not str(it.get("first_name") or "").strip():
            out.append("a text for the named templates with no first name")
        elif named:
            out.append(junk_greeting(it.get("first_name"), it.get("last_name"), raw_printed=True))
    elif kind == "heir_text":
        out.append(junk_greeting(it.get("first_name")))
    elif kind == "drip" and it.get("do") == "text":
        out.append(junk_greeting(it.get("first")))
    elif kind in MAIL_KINDS:
        out.append(junk_envelope(it.get("name")))
        out.append(junk_greeting(it.get("first_name")))
    return [x for x in out if x]


def address_problem(it: dict) -> str:
    street, city = str(it.get("street") or "").strip(), str(it.get("city") or "").strip()
    state, zip5 = str(it.get("state") or "").strip(), str(it.get("zip") or "").strip()
    if not (street and city and state):
        return "an incomplete mailing address"
    if not (len(zip5) == 5 and zip5.isdigit()):
        return "a mailing ZIP that is not five digits"
    return ""


def label(kind: str, n: int) -> str:
    from .. import release
    return "%d %s" % (n, release.label(kind))


# ---------------------------------------------------------------------- the checks
def _norm(s: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(s or "").lower()).split())


def readback(crm, kind: str, it: dict) -> dict:
    """The first item's record read again and set beside what the batch holds."""
    from ..autopilot import gate
    u = it.get("record_uuid") or ""
    out = {"record": u[:8], "ok": False, "differs": []}
    try:
        rec = crm.record(u) if u else {}
    except Exception as exc:  # noqa: BLE001  (a read that fails is the finding)
        out["differs"].append("the record could not be read back (%s)" % str(exc)[:80])
        return out
    if not rec or not rec.get("uuid"):
        out["differs"].append("the record is not in the account any more")
        return out
    out["rec"] = rec
    want = street_of(it)
    have = ((rec.get("address") or {}).get("street") or "") if isinstance(rec.get("address"), dict) else ""
    if want and have and not _norm(want).startswith(_norm(have)) and not _norm(have).startswith(_norm(want)):
        out["differs"].append("the street on the record is %s, the batch says %s" % (have, want))
    ow = rec.get("owner") or {}
    if kind == "text" and (it.get("first_name") or it.get("last_name")):
        if (_norm(it.get("first_name")), _norm(it.get("last_name"))) != (_norm(ow.get("first_name")), _norm(ow.get("last_name"))) \
                and not (it.get("last_name") and _norm(it.get("last_name")) == _norm(ow.get("company"))):
            out["differs"].append("the owner's name on the record changed since the batch was staged")
    if kind == "mail":
        have_name = _norm(" ".join(x for x in (ow.get("first_name"), ow.get("last_name")) if x) or ow.get("company"))
        if it.get("name") and have_name and _norm(it.get("name")) != have_name:
            out["differs"].append("the owner's name on the record changed since the batch was staged")
        ma = ow.get("address") or {}
        if it.get("street") and ma.get("street") and _norm(it["street"]) != _norm(ma.get("street")):
            out["differs"].append("the owner's mailing street changed since the batch was staged")
    have_nums = {gate.digits(p.get("number")) for p in ow.get("phones") or []}
    nums = numbers_of(kind, it)
    gone = [n for n in nums if n not in have_nums]
    if gone:
        out["differs"].append("%d of %d number(s) are no longer on the owner (ending %s)" % (
            len(gone), len(nums), ", ".join(n[-4:] for n in gone[:3])))
    out["ok"] = not out["differs"]
    return out


def _litigators(rec: dict) -> set:
    from ..autopilot import gate, ledger
    out = set()
    for p in (rec.get("owner") or {}).get("phones") or []:
        n = gate.digits(p.get("number"))
        c = ledger.phone(n) if n else None
        if c and c.get("litigator"):
            out.add(n)
    return out


def verdict(rec: dict, channel: str, pol, stage) -> object:
    """The gate the way the stage that staged the batch runs it: its own gate_allow, the owner's
    lane, the scoring cache's litigators and the custom fields already read this week (no new read)."""
    from datetime import timedelta as _td
    from ..autopilot import gate, lanes, ledger
    cfg = getattr(pol, "gate", None) or gate.GateConfig()
    allow = list((stage.get("gate_allow") or []) if stage is not None else [])
    custom = ledger.fact(rec.get("uuid") or "", "custom_fields", max_age=_td(days=7)) if rec.get("uuid") else None
    lane = str(stage.get("lane") or "") if stage is not None else ""
    return gate.check(rec, channel, cfg=cfg, lane=lane, allow=allow, custom=custom,
                      owner=lanes.owner_lane((rec.get("owner") or {}).get("uuid") or ""),
                      litigators=_litigators(rec), lanes_cfg=getattr(pol, "lanes", None))


def _phone_on(rec: dict, n: str) -> dict | None:
    from ..autopilot import gate
    return next((p for p in (rec.get("owner") or {}).get("phones") or [] if gate.digits(p.get("number")) == n), None)


def qa_one(b: dict, crm, pol, now: datetime, reads: dict) -> dict:
    from ..autopilot import gate, ledger, limits, scrub
    kind, items = b["kind"], b.get("items") or []
    tz = getattr(pol, "tz", "") or TZ
    today = now.astimezone(ZoneInfo(tz)).date().isoformat()
    stage = None
    try:
        stage = pol.stage(b["stage"]) if pol is not None else None
    except KeyError:
        stage = None
    found: Counter = Counter()
    ex: dict[str, list] = defaultdict(list)
    said: set = set()

    def flag(what: str, example: str = "", key=None) -> None:
        """Count one finding. `key` says when two checks found the same thing (a landline the
        scoring cache and the record both name): it is counted once."""
        if key is not None:
            if (what, key) in said:
                return
            said.add((what, key))
        found[what] += 1
        if example and len(ex[what]) < 3 and example not in ex[what]:
            ex[what].append(example)

    touch = kind not in NOT_TOUCHES
    # ---- duplicates, inside the batch and against every other staged batch
    elsewhere_recs, elsewhere_nums = {}, {}
    for o in ledger.batches(status="staged"):
        if o["id"] == b["id"] or o["kind"] in NOT_TOUCHES:
            continue
        full = ledger.batch(o["id"]) or {}
        for it in full.get("items") or []:
            if it.get("record_uuid"):
                elsewhere_recs.setdefault((it["record_uuid"], channel_of(o["kind"], it)), o["id"])
            for n in numbers_of(o["kind"], it):
                elsewhere_nums.setdefault(n, o["id"])
    recs, owners, nums_seen = Counter(), Counter(), Counter()
    for it in items:
        recs[(it.get("record_uuid") or "", it.get("heir") or "", it.get("step") or 0, it.get("letter") or 0)] += 1
        if channel_of(kind, it) == "text" and it.get("owner_uuid"):
            owners[it["owner_uuid"]] += 1
        for n in numbers_of(kind, it):
            nums_seen[n] += 1
    stale = set()
    for it in items:
        where = street_of(it) or (it.get("record_uuid") or "")[:8]
        ch = channel_of(kind, it)
        key = (it.get("record_uuid") or "", it.get("heir") or "", it.get("step") or 0, it.get("letter") or 0)
        if recs[key] > 1:
            flag("the same record is in the batch twice", where)
        if ch == "text" and it.get("owner_uuid") and owners[it["owner_uuid"]] > 1:
            flag("one owner gets more than one text in the batch", where)
        if touch and (it.get("record_uuid"), ch) in elsewhere_recs:
            flag("the record also waits in another staged batch", "%s (batch %d)" % (where, elsewhere_recs[(it.get("record_uuid"), ch)]))
        nums = numbers_of(kind, it)
        for n in nums:
            if nums_seen[n] > 1:
                flag("the same number is in the batch twice", "ending %s" % n[-4:])
            if n in elsewhere_nums:
                flag("a number also waits in another staged batch", "ending %s (batch %d)" % (n[-4:], elsewhere_nums[n]))
            if ch == "text" and scrub.is_flagged(n):
                flag("a number on the do-not-call registry (never texted)", "ending %s" % n[-4:])
            lt = str((ledger.phone(n) or {}).get("line_type") or "").strip().lower()
            if ch == "text" and lt in NOT_MOBILE:
                flag("a landline or other line that cannot take a text", "ending %s (%s)" % (n[-4:], lt),
                     key=(it.get("record_uuid"), n))
        for why in names_of(kind, it):
            flag("a name that should not reach a person: %s" % why, where)
        if kind in MAIL_KINDS:
            p = address_problem(it)
            if p:
                flag(p, where)
        if kind in TEXT_KINDS and it.get("day") and it["day"] != today:
            stale.add(it["day"])
        if ch == "text" and it.get("owner_uuid"):
            sends = [n.get("send_at") for n in it.get("numbers") or [] if isinstance(n, dict) and n.get("send_at")] \
                or ([it["send_at"]] if it.get("send_at") else [])
            try:
                when = min(datetime.fromisoformat(s) for s in sends) if sends else now
            except ValueError:
                when = now
            r = limits.room(it["owner_uuid"], "text", max(when, now), policy=pol, lim=limits.settings(pol), tz=tz,
                            number=(nums[0] if kind != "text" and nums else ""), system=str(it.get("system") or ""),
                            campaign=kind != "drip", exclude_batch=int(b["id"]))
            # The sending hours are the release's to judge (it reads its own clock), and a registry
            # number already has its own line above: neither is said twice.
            if not r.ok and r.code not in ("hours", "registry"):
                flag("the house limits refuse it: %s" % r.why, where)
    if stale:
        flag("staged for %s: a text batch is released on its day or not at all" % ", ".join(sorted(stale)))

    # ---- reads: the first record back, then the gate on as many records as the budget allows
    rb = {"record": "", "ok": None, "differs": []}
    read_n = 0
    if items:
        rb = readback(crm, kind, items[0])
        reads["left"] -= 1
        read_n = 1
        for d in rb["differs"]:
            flag("read-back of the first record: %s" % d, street_of(items[0]))
    if touch:
        cache = {(items[0].get("record_uuid") or ""): rb.get("rec")} if items else {}
        for it in items:
            u = it.get("record_uuid") or ""
            ch = channel_of(kind, it)
            if not u or not ch:
                continue
            rec = cache.get(u)
            if rec is None:
                if reads["left"] <= 0:
                    continue
                reads["left"] -= 1
                read_n += 1
                try:
                    rec = crm.record(u) or {}
                except Exception as exc:  # noqa: BLE001
                    flag("a record could not be read (%s)" % str(exc)[:60], street_of(it))
                    rec = {}
                cache[u] = rec
            if not rec.get("uuid"):
                continue
            v = verdict(rec, ch, pol, stage)
            if not v.ok and v.code != "no_number":
                flag("the gate now holds the record: %s" % v.reason(), street_of(it))
            usable = {x["number"] for x in v.numbers}
            for n in numbers_of(kind, it):
                p = _phone_on(rec, n)
                if p is None:
                    continue           # said once by the read-back for the first record; the release drops it
                st = str(p.get("status") or "UNKNOWN").upper()
                if st in gate.BAD_PHONE:
                    flag("a number marked wrong, dead or do-not-call", "ending %s" % n[-4:])
                elif ch == "text" and str(p.get("type") or "").upper() != "MOBILE":
                    flag("a landline or other line that cannot take a text",
                         "ending %s (%s)" % (n[-4:], str(p.get("type") or "no type").lower()), key=(u, n))
                elif n not in usable:
                    flag("a number the gate does not allow (not a top-tier or confirmed number)", "ending %s" % n[-4:])
    checkable = sum(1 for it in items if it.get("record_uuid") and channel_of(kind, it)) if touch else (1 if items else 0)
    return {"batch": b["id"], "kind": kind, "stage": b.get("stage") or "", "items": len(items), "what": label(kind, len(items)),
            "created": str(b.get("created") or "")[:16].replace("T", " "), "touch": touch, "findings": dict(found),
            "examples": {k: v for k, v in ex.items()}, "readback": {k: v for k, v in rb.items() if k != "rec"},
            "read": read_n, "checkable": checkable}


# ---------------------------------------------------------------------- the run
def _sig(items) -> str:
    return hashlib.sha256(json.dumps(items or [], sort_keys=True, default=str).encode()).hexdigest()[:16]


def run(task: dict) -> dict:
    from ..autopilot import ledger
    payload = store.payload_of(task)
    want = str((payload.get("inputs") or {}).get("batch") or "all").strip().lower().lstrip("#")
    want = want.replace("batch", "").strip() or "all"
    scheduled = (task.get("source") or "") == "schedule"
    pol, now = _policy(), _now()
    out = {"workflow": WORKFLOW, "asked": want, "batches": [], "unchanged": 0, "files": [], "run_dir": "", "problems": []}
    if want.isdigit():
        b = ledger.batch(int(want))
        if not b:
            out["problems"].append("there is no batch %s" % want)
            return out
        if b["status"] != "staged":
            out["problems"].append("batch %s is %s, so there is nothing waiting to check" % (want, b["status"]))
            return out
        if b.get("kind") == "command":
            out["problems"].append("batch %s is a command batch: it reaches no homeowner, and it is checked "
                                   "against the script catalog when it is released" % want)
            return out
        todo = [b]
    elif want == "all":
        todo = [ledger.batch(x["id"]) for x in ledger.batches(status="staged") if x.get("kind") != "command"]
    else:
        out["problems"].append("%r is not a batch number; ask for a number from the digest, or all" % want)
        return out
    crm = _crm()
    reads = {"left": max_reads()}
    for b in [x for x in todo if x]:
        sig = _sig(b.get("items"))
        if scheduled and store.kv_get("wf:qa_batch:%d" % b["id"]) == sig:
            out["unchanged"] += 1
            continue
        out["batches"].append(qa_one(b, crm, pol, now, reads))
        store.kv_set("wf:qa_batch:%d" % b["id"], sig)
    if out["batches"]:
        p = _write(task, out["batches"])
        out["files"], out["run_dir"] = [p.name], str(p.parent.parent)
    return out


def _write(task: dict, results: list[dict]) -> Path:
    d = Path(config.RUNS_DIR) / ("%s-%d" % (WORKFLOW, int(task.get("id") or 0))) / "files"
    d.mkdir(parents=True, exist_ok=True)
    md = ["# Batch check", "", "Nothing here was released. Each batch is still staged; its release checks every item again.", ""]
    for r in results:
        md += ["## Batch %d: %s from %s, staged %s" % (r["batch"], r["what"], r["stage"].replace("_", " "), r["created"]), ""]
        rb = r["readback"]
        if rb.get("record"):
            md.append("Read-back of the first record (%s): %s." % (rb["record"], "matches the batch" if rb.get("ok")
                                                                 else "; ".join(rb.get("differs") or [])))
        if r["touch"]:
            md.append("Records checked against the gate: %d of %d (the rest are checked at release)." % (
                min(r["read"], r["checkable"]), r["checkable"]))
        else:
            md.append("Not a touch, so the gate does not apply: read-back and duplicates only.")
        md.append("")
        if not r["findings"]:
            md += ["Nothing to look at.", ""]
            continue
        for what, n in sorted(r["findings"].items(), key=lambda kv: -kv[1]):
            e = r["examples"].get(what) or []
            md.append("- %d: %s%s" % (n, what, (" (for example %s)" % ", ".join(e)) if e else ""))
        md.append("")
    p = d / ("qa_batch_%s.md" % "_".join(str(r["batch"]) for r in results[:6]))
    p.write_text("\n".join(md), encoding="utf-8")
    return p


def _short(findings: dict) -> str:
    top = sorted(findings.items(), key=lambda kv: -kv[1])[:2]
    words = []
    for what, n in top:
        w = what.split(": ", 1)[-1] if what.startswith("a name that should not") else what
        words.append("%d %s" % (n, w))
    return "; ".join(words)


def render(task: dict, result: dict) -> str:
    probs = result.get("problems") or []
    rs = result.get("batches") or []
    if probs and not rs:
        return ":mag: %s." % (probs[0][0].upper() + probs[0][1:])
    if not rs:
        if (task.get("source") or "") == "schedule":
            return ""
        return ":mag: No staged batch is waiting on a release%s." % (
            " that changed since it was last checked" if result.get("unchanged") else "")
    lines = []
    for r in rs[:3]:
        n = sum(r["findings"].values())
        rb = r["readback"]
        back = "" if not rb.get("record") else (" The first record reads back the same." if rb.get("ok") else "")
        if n:
            lines.append(":mag: Batch %d, %s from %s: %d to look at (%s).%s" % (
                r["batch"], r["what"], r["stage"].replace("_", " "), n, _short(r["findings"]), back))
        else:
            lines.append(":mag: Batch %d, %s from %s: clean.%s %d of %d checked against the gate." % (
                r["batch"], r["what"], r["stage"].replace("_", " "), back, min(r["read"], r["checkable"]), r["checkable"])
                if r["touch"] else ":mag: Batch %d, %s: clean.%s" % (r["batch"], r["what"], back))
    if len(rs) > 3:
        lines.append("And %d more batch(es) in the detail." % (len(rs) - 3))
    for name in result.get("files") or []:
        url = slack.file_url(task.get("id") or 0, name)
        if url:
            lines.append("Detail: %s" % url)
    lines.append("Nothing was released; a person still releases each batch.")
    return "\n".join(lines)
