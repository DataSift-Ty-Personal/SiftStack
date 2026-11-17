"""Probate kept honest (build plan 3.8): a change list a person approves.

Three things were wrong with the probate lane, each measured:

  * WRONG HOUSES. The address audit of 2026-11-11 compared every Probate-list record with the
    county's owner of record and flagged 164. A flag is not proof: of four flagged records
    researched afterwards, three were the decedent's real home with a later change of title, and
    one was the executor's own residence. The split that needs no research is the one applied
    here. The representative's own home (they live at that address) comes out. A house with an
    unrelated owner and no recent transfer comes out. A house an unrelated owner bought since
    mid-2025 was sold by the estate and is marked. A house the representative owns and does not
    live in was most likely inherited, and stays. A spelling variant stays.
  * OLD FILINGS. A probate filed more than 24 months ago drops to mail only.
  * A NO IS NOT FOREVER. A probate record that said no comes back after 45 days as a task for the
    prospecting caller, never the lead manager, to call only the number a person confirmed. Each
    repeat is timed from the last re-call. When the playbook runs a second-chance stage, that
    stage writes every re-call (probate is one row of its table) and this one only checks that
    the wait and the callers match what was decided here: two stages writing the same re-call
    would hand a caller the same owner twice. With no such stage, the re-calls are part of this
    list.

NEVER A SILENT EDIT. This stage writes nothing to the account by itself, in any mode. In plan mode
it says the list. On a live box it stages the list as one batch and stops: a person reads it
(the file is written beside the batch) and approves it with `cli.py autopilot release <id>
--commit`. Only then is each record read again and changed, and every change is recorded with the
value it replaced. While a list waits, no second list is staged.

What "comes out" writes is a setting, because the decision (D35) is the owner's. By default the
record loses the first-to-market tag and the Probate list and gains the tag Wrong House, each a
documented route. A house the estate sold gains the tag Estate Sold. The `recently sold` tag is
never written here: sold_guard is the only thing that hands that tag out, and its rule (sold after
the record became a lead, read from the record's own last sale) is not the audit's.

The filing date is read off each record, never asked of the search: the search silently ignores a
filter key it does not know, and a date filter that matched every probate record would put the
whole list on mail only.

BLOUNT COUNTY: the audit already covers Blount records (it read the state assessor's site), so the
split applies there as it does in Knox. No obituary index is involved in anything this stage does.
"""
from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import activity, gate, lanes, ledger, researchqueue, researchroutes
from .stages import GONE_STATUSES, Ctx, Summary, addr, assignee_of, resolve_callers, tag_titles

log = logging.getLogger(__name__)

KIND = "probate"
FACT = "probate_facts"            # the filing date on a probate record, read once
MONTH_TAG = re.compile(r"^(20\d\d)-(\d\d)$")

DEFAULTS = {
    "audit_csv": "",                # default: the newest probate_address_audit_*.csv (the box's data folder, then output/)
    "audit_actions": {"own_home": "out", "unrelated": "out", "sold": "sold", "inherited": "keep",
                      "name_variant": "keep"},                        # decision D35, as recommended
    "out": {"add_tags": ["Wrong House"], "remove_tags": ["FTM"], "remove_lists": ["Probate"]},
    "sold": {"add_tags": ["Estate Sold"], "remove_tags": ["FTM"], "remove_lists": ["Probate"]},
    "mail_only_months": 24,
    "mail_only_tag": None,          # default: the gate's Mail Only tag
    "recall": "auto",               # auto: left to the playbook's second-chance stage when it has one, else listed here
    "recall_days": 45,              # decision D23, as recommended for probate
    "recall_callers": None,         # the prospecting callers; default: people.callers
    "not_callers": [],              # people who never get the re-call (the lead manager)
    "scope_lists": None,            # default: the gate's probate marks
    "probate_date_fields": ["Probate Open Date"],
    "max_reads": 150,
    "max_scan": 3000,
    "max_log_calls": 60,
}

AUDIT_WHY = {
    "own_home": "the representative lives at this address: it is their own home, not the estate's house",
    "unrelated": "the county shows an unrelated owner and no recent transfer: a wrong house",
    "sold": "the county shows an unrelated owner who bought it since mid-2025: the estate sold it",
    "inherited": "the representative owns it and lives elsewhere: most likely inherited, and the lead to work",
    "name_variant": "the owner on the county roll is a spelling of the decedent's name: fine as it is",
}
ACTION_WORDS = {"out": "comes out", "sold": "marked sold", "keep": "stays", "mail_only": "mail only",
                "task": "re-call task", "left": "left for a person"}


def options(st) -> dict:
    o = {}
    for k, v in DEFAULTS.items():
        got = st.get(k)
        o[k] = v if got is None else got
    return o


# ---------------------------------------------------------------------- the address audit
def classify(label: str) -> str:
    """The audit's own suggestion, as one of five keys, or '' for a record it did not flag."""
    s = " ".join(str(label or "").lower().split())
    if not s:
        return ""
    if s.startswith("wrong house") and "own home" in s:
        return "own_home"
    if s.startswith("wrong house"):
        return "unrelated"
    if "sold" in s:
        return "sold"
    if s.startswith("work it") or "lives elsewhere" in s:
        return "inherited"
    if "name variant" in s or s.startswith("likely fine"):
        return "name_variant"
    return "unknown"


def find_audit(o: dict) -> Path | None:
    """The audit file: the one the playbook names, else the newest on this box."""
    if o["audit_csv"]:
        p = Path(str(o["audit_csv"]))
        return p if p.is_file() else None
    found: list[Path] = []
    for folder in (Path(config.DATA_DIR) / "probate", config.ROOT / "output"):
        if folder.is_dir():
            found += sorted(folder.glob("probate_address_audit_*.csv"))
        if found:
            break
    return found[-1] if found else None


def load_audit(path: Path) -> list[dict]:
    """The flagged rows of the audit: record id, the key its suggestion maps to, and the street
    (for the list a person reads). Nothing else is taken from the file."""
    out = []
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            key = classify(row.get("suggested"))
            u = str(row.get("uuid") or "").strip()
            if key and u:
                out.append({"record_uuid": u, "key": key, "street": " ".join(str(row.get("street") or "").split())})
    return out


def settled(record_uuid: str, part: str = "audit") -> bool:
    return any(e["detail"].get("part") == part for e in ledger.events(record_uuid, KIND))


def flagged_left(path: Path | None) -> tuple[int, int]:
    """(flagged in the audit, still not settled): the number the plan wants at zero."""
    if not path:
        return 0, 0
    rows = load_audit(path)
    return len(rows), sum(1 for r in rows if not settled(r["record_uuid"]))


# ---------------------------------------------------------------------- the filing date
def probate_day(rec: dict, custom: dict | None, o: dict) -> tuple[str, str]:
    """(the filing date, where it was read), or ('', ''). The record's own field first, then the
    custom field, then the earliest notice month tag."""
    d = researchqueue.iso_day(rec.get("probate_open_date"))
    if d:
        return d, "the probate open date"
    low = {str(k).strip().lower(): v for k, v in (custom or {}).items()}
    for label in o["probate_date_fields"]:
        d = researchqueue.iso_day(low.get(str(label).strip().lower()))
        if d:
            return d, "the %s field" % label
    months = sorted(t for t in tag_titles(rec) if MONTH_TAG.match(t))
    if months:
        return months[0] + "-01", "the notice month tag"
    return "", ""


def months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) - (1 if end.day < start.day else 0)


# ---------------------------------------------------------------------- building the list
def _writes(o: dict, action: str) -> dict:
    block = o.get(action) if action in ("out", "sold") else {}
    block = block if isinstance(block, dict) else {}
    return {k: [str(x) for x in (block.get(k) or [])] for k in ("add_tags", "remove_tags", "remove_lists")}


def audit_items(ctx: Ctx, o: dict, path: Path, budget: dict) -> list[dict]:
    out = []
    actions = dict(DEFAULTS["audit_actions"], **(o["audit_actions"] if isinstance(o["audit_actions"], dict) else {}))
    for row in load_audit(path):
        u, key = row["record_uuid"], row["key"]
        if settled(u):
            continue
        if key == "unknown":
            ctx.out.tally["audit suggestion this stage does not know: left for a person"] += 1
            continue
        action = str(actions.get(key) or "keep")
        if action not in ("out", "sold", "keep"):
            ctx.out.problem("audit_actions.%s is %r; use out, sold or keep" % (key, action))
            continue
        if budget["reads"] >= budget["max"]:
            ctx.out.tally["flagged records not read yet (read cap, next run)"] += 1
            continue
        budget["reads"] += 1
        rec = ctx.crm.record(u)
        street = row["street"] or addr(rec)
        item = {"record_uuid": u, "street": street, "part": "audit", "key": key, "action": action, "why": AUDIT_WHY[key],
                "add_tags": [], "remove_tags": [], "remove_lists": [], "note": ""}
        if not rec.get("uuid"):
            item.update(action="left", why="the record could not be read: it may have been merged or removed")
        elif lanes.status_of(rec) in lanes.LEAD_STATUSES:
            item.update(action="left", why="flagged (%s), but the record is a lead now: the lead manager decides" % AUDIT_WHY[key])
        elif action in ("out", "sold"):
            item.update(_writes(o, action))
            item["note"] = "Address audit: %s. %s." % (AUDIT_WHY[key], "Taken out of the probate lane"
                                                       if action == "out" else "Marked as sold by the estate")
        else:
            item["note"] = "Address audit: %s. Kept in the probate lane." % AUDIT_WHY[key]
        out.append(item)
    return out


def _probate_rows(ctx: Ctx, o: dict, gate_cfg, extra: dict | None = None) -> list[dict]:
    seen, out = set(), []
    for name in (o["scope_lists"] if o["scope_lists"] is not None else gate_cfg.probate_marks):
        lid = ctx.crm.list_uuid(str(name))
        if not lid:
            ctx.out.problem("there is no %r list in this account, so no probate record is read from it" % name)
            continue
        must = dict({"any_lists": [lid]}, **(extra or {"must_not": {"any_property_status": GONE_STATUSES}}))
        for row in ctx.crm.rows(must, cap=int(o["max_scan"])):
            if row.get("uuid") and row["uuid"] not in seen:
                seen.add(row["uuid"])
                out.append(row)
    return out


def age_items(ctx: Ctx, o: dict, gate_cfg, today: date, budget: dict) -> list[dict]:
    """Probates filed more than `mail_only_months` ago that are not mail only yet."""
    tag = str(o["mail_only_tag"] or (gate_cfg.mail_only_tags[0] if gate_cfg.mail_only_tags else "Mail Only"))
    limit = int(o["mail_only_months"])
    out = []
    for row in _probate_rows(ctx, o, gate_cfg):
        u = row["uuid"]
        if lanes.status_of(row) in lanes.LEAD_STATUSES:
            continue
        f = ledger.fact(u, FACT)
        if f is None:
            if budget["reads"] >= budget["max"]:
                ctx.out.tally["probate records whose filing date is not read yet (read cap, next run)"] += 1
                continue
            budget["reads"] += 1
            rec = ctx.crm.record(u)
            if not rec.get("uuid"):
                continue
            day, source = probate_day(rec, None, o)
            if not day:
                day, source = probate_day(rec, researchqueue.custom_of(ctx, u, max_age=timedelta(days=7)), o)
            f = {"day": day, "source": source, "at": ctx.now.isoformat(timespec="seconds"),
                 "mail_only": tag.lower() in {t.lower() for t in tag_titles(rec)}}
            ledger.set_fact(u, FACT, f)          # a filing date does not change: knowledge, kept in plan mode too
        if not f.get("day"):
            ctx.out.tally["no filing date on the record: left alone"] += 1
            continue
        months = months_between(date.fromisoformat(f["day"]), today)
        if months < limit:
            continue
        if f.get("mail_only") or settled(u, "age"):
            ctx.out.tally["older than %d months, already mail only" % limit] += 1
            continue
        when = date.fromisoformat(f["day"]).strftime("%B %Y")
        out.append({"record_uuid": u, "street": addr(row), "part": "age", "key": "old", "action": "mail_only",
                    "why": "filed %s, %d months ago (read from %s)" % (when, months, f.get("source") or "the record"),
                    "add_tags": [tag], "remove_tags": [], "remove_lists": [],
                    "note": "Probate filed %s, more than %d months ago: mail only from here." % (when, limit)})
    return out


def said_no(ctx: Ctx, record_uuid: str) -> datetime:
    """When the record last became Not Interested: the change in the shared log, else the first time
    this stage saw it in that status (a later date, so the wait is never cut short)."""
    for e in reversed(activity.events("property.status.updated", resource=record_uuid)):
        if str(e["data"].get("to") or "").strip().lower() == "not_interested":
            return e["when"]
    return ledger.first_seen(record_uuid, "probate_no", ctx.now)


def last_recall(record_uuid: str) -> tuple[datetime | None, str]:
    """(when the last re-call was written, its task id)."""
    done = [e for e in ledger.events(record_uuid, KIND) if e["detail"].get("part") == "recall"]
    if not done:
        return None, ""
    return datetime.fromisoformat(done[-1]["at"]), str(done[-1]["detail"].get("task") or "")


def confirmed_number(rec: dict) -> str:
    """The number a person marked correct, the only one a second call may use."""
    for p in ((rec.get("owner") or {}).get("phones") or []):
        if str(p.get("status") or "").upper() == "CORRECT":
            n = gate.digits(p.get("number"))
            if n:
                return n
    return ""


def second_chance_stage(ctx: Ctx):
    """The playbook's own second-chance stage, when it has one that is switched on. That stage calls
    every kind of no back on a clock by list, probate included, so two stages writing the same
    re-call would hand a caller the same owner twice."""
    for s in (getattr(ctx.policy, "stages", None) or {}).values():
        if getattr(s, "kind", "") == "secondchance" and getattr(s, "mode", "") != "off":
            return s
    return None


def check_second_chance(ctx: Ctx, o: dict, other) -> None:
    """The re-call belongs to the second-chance stage: say so, and check that it treats a probate
    no the way this lane was decided (the wait, and who never gets the task)."""
    days = None
    try:
        from . import secondchance
        _, days = secondchance.interval_for(other, {"probate"})
    except Exception:  # noqa: BLE001  (that stage's own table could not be read: only the handover is said)
        days = None
    want = int(o["recall_days"])
    if days is not None and int(days) != want:
        ctx.out.problem("the %s stage calls a probate no back after %d days; this lane's rule is %d" % (other.id, int(days), want))
    never = {str(n).strip().lower() for n in (o["not_callers"] or [])}
    names = other.get("callers") or getattr(ctx.policy, "callers", None) or []
    named = [str(n.get("name") if isinstance(n, dict) else n) for n in names]
    bad = [n for n in named if n.strip().lower() in never]
    if bad:
        ctx.out.problem("the %s stage gives re-calls to %s, who is not to get them (not_callers)" % (other.id, ", ".join(bad)))
    ctx.out.line("A probate record that said no is called back by the %s stage%s; this stage writes no re-call." % (
        other.id, (" after %d days" % int(days)) if days is not None else ""))


def recall_items(ctx: Ctx, o: dict, gate_cfg, budget: dict) -> list[dict]:
    """Probate records that said no at least `recall_days` ago: one task each, for a prospecting
    caller, to call only the confirmed number."""
    wait = timedelta(days=float(o["recall_days"]))
    people = resolve_callers(ctx, list(o["recall_callers"])) if o["recall_callers"] else resolve_callers(ctx)
    never = {str(n).strip().lower() for n in (o["not_callers"] or [])}
    pool = [p for p in people if p["name"].strip().lower() not in never]
    rows = _probate_rows(ctx, o, gate_cfg, extra={"any_property_status": ["not_interested"]})
    if rows and not pool:
        ctx.out.problem("no prospecting caller to give a re-call to (%d probate record(s) said no)" % len(rows))
        return []
    out = []
    for row in rows:
        u = row["uuid"]
        other = ledger.fact(u, "second_chance") or {}
        if other.get("task") or other.get("created") or other.get("last_recall"):
            ctx.out.tally["already handled by the second-chance stage"] += 1
            continue
        base = said_no(ctx, u)
        again, task = last_recall(u)
        if again is not None:
            # Each repeat is timed from the last re-call, and one still open is never doubled.
            if task and ctx.now - again < wait * 3:
                if budget["reads"] >= budget["max"]:
                    ctx.out.tally["re-calls not read yet (read cap, next run)"] += 1
                    continue
                budget["reads"] += 1
                try:
                    still = researchroutes.task_open(ctx.crm, task)
                except Exception as exc:  # noqa: BLE001
                    ctx.out.problem("could not read a re-call task (%s); that record waits a run" % str(exc)[:80])
                    continue
                if still:
                    ctx.out.tally["the last re-call task is still open"] += 1
                    continue
            base = max(base, again)
        if ctx.now - base < wait:
            ctx.out.tally["said no less than %d days ago" % int(o["recall_days"])] += 1
            continue
        if budget["reads"] >= budget["max"]:
            ctx.out.tally["re-calls not read yet (read cap, next run)"] += 1
            continue
        budget["reads"] += 1
        rec = ctx.crm.record(u)
        if not rec.get("uuid"):
            continue
        # The second-chance lift, and only for this one action: nothing else here may touch a no.
        v = ctx.verdict(rec, "task", row=row, allow={"not_interested"})
        if not v.ok:
            ctx.out.tally["re-call held back: %s" % v.reason()] += 1
            continue
        number = confirmed_number(rec)
        if not number:
            ctx.out.tally["said no, but no confirmed number to call again"] += 1
            continue
        mine = next((p for p in pool if p["uuid"] == assignee_of(rec)), None)
        who = mine or pool[len(out) % len(pool)]
        days = int((ctx.now - base).total_seconds() // 86400)
        out.append({"record_uuid": u, "street": addr(row), "part": "recall", "key": "no", "action": "task",
                    "why": "said no %d days ago" % days, "add_tags": [], "remove_tags": [], "remove_lists": [], "note": "",
                    "task": {"title": "Call again: %s. Said no %d days ago. Call only the confirmed number ending %s." % (
                        addr(row), days, researchroutes.last4(number)),
                        "user_uuid": who["uuid"], "user": who["name"],
                        "notes": "A second call on a probate record that said no. Use the confirmed number only."}})
    return out


def export(batch_id: int, items: list[dict]) -> str:
    """The change list as a file a person can read before approving it."""
    out = Path(config.DATA_DIR) / "probate" / ("changes_%d.csv" % batch_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["street", "part", "action", "why", "add_tags", "remove_tags", "remove_lists", "task", "record_uuid"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for it in items:
            row = dict(it)
            for k in ("add_tags", "remove_tags", "remove_lists"):
                row[k] = ", ".join(it.get(k) or [])
            row["action"] = ACTION_WORDS.get(it.get("action"), it.get("action"))
            row["task"] = ("%s -> %s" % ((it.get("task") or {}).get("title", ""), (it.get("task") or {}).get("user", ""))
                           if it.get("task") else "")
            w.writerow(row)
    return str(out)


def staged_batch(stage_id: str) -> dict | None:
    for b in ledger.batches(status="staged", stage=stage_id):
        if b["kind"] == KIND:
            return b
    return None


# ---------------------------------------------------------------------- the stage
def run_probate(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    gate_cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    path = find_audit(o)
    flagged, left = flagged_left(path)
    if path:
        ctx.out.tally["flagged in the address audit"] = flagged
        ctx.out.tally["flagged and not settled yet"] = left
    else:
        ctx.out.line("No address audit file on this box (probate_address_audit_<day>.csv under %s): the wrong-house "
                     "split waits for it; the age rule and the re-calls still run." % (Path(config.DATA_DIR) / "probate"))
    waiting = staged_batch(st.id)
    if waiting:
        ctx.out.found = waiting["count"]
        ctx.out.head = ("The probate change list (batch %d, %d change(s)) is still waiting on a person. Nothing was "
                        "changed. Read it, then: python src/agents/cli.py autopilot release %d --commit" % (
                            waiting["id"], waiting["count"], waiting["id"]))
        return
    try:
        activity.sync(ctx.crm, "state", now=ctx.now, max_calls=int(o["max_log_calls"]))
    except Exception as exc:  # noqa: BLE001
        ctx.out.problem("could not read the change log (%s); a no is timed from the day this stage first saw it" % str(exc)[:80])
    budget = {"reads": 0, "max": int(o["max_reads"])}
    items = audit_items(ctx, o, path, budget) if path else []
    items += age_items(ctx, o, gate_cfg, today, budget)
    mode = str(o["recall"]).strip().lower()
    other = second_chance_stage(ctx) if mode == "auto" else None
    if mode not in ("auto", "list", "off"):
        ctx.out.problem("recall must be auto, list or off; %r is read as off" % o["recall"])
    elif other is not None:
        check_second_chance(ctx, o, other)
    elif mode in ("auto", "list"):
        items += recall_items(ctx, o, gate_cfg, budget)
    ctx.out.found = len(items)
    by = Counter(ACTION_WORDS.get(it["action"], it["action"]) for it in items)
    for it in items[:6]:
        ctx.out.line("%s: %s (%s)" % (it["street"], ACTION_WORDS.get(it["action"], it["action"]), it["why"]))
    mix = ", ".join("%d %s" % (n, w) for w, n in by.most_common())
    if not items:
        ctx.out.head = "The probate lane has nothing to settle today."
        return
    ctx.out.acted = len(items)
    if not ctx.live:
        ctx.out.head = "Would list %d probate change(s) for a person to approve: %s. Nothing is changed without approval." % (
            len(items), mix)
        return
    bid = ledger.batch_create(st.id, KIND, items, 0)
    where = export(bid, items)
    ctx.out.batch = bid
    ctx.out.head = ("Listed %d probate change(s) as batch %d: %s. Nothing was changed. Read %s, then approve: "
                    "python src/agents/cli.py autopilot release %d --commit" % (len(items), bid, mix, where, bid))


# ---------------------------------------------------------------------- approval
def apply_one(ctx: Ctx, it: dict, state: dict) -> str:
    """Apply one approved change. The record is read first: the list can be days old."""
    u = it["record_uuid"]
    part, action = it.get("part"), it.get("action")
    rec = ctx.crm.record(u)
    detail = {"part": part, "key": it.get("key"), "action": action, "why": it.get("why")}
    if action == "left" or not rec.get("uuid"):
        ledger.event(ctx.run_id, ctx.stage.id, u, KIND, dict(detail, action="left"), now=ctx.now, tz=ctx.policy.tz)
        return "left for a person"
    if lanes.status_of(rec) in lanes.LEAD_STATUSES:
        ledger.event(ctx.run_id, ctx.stage.id, u, KIND, dict(detail, action="left", why="a lead by the time the list was approved"),
                     now=ctx.now, tz=ctx.policy.tz)
        return "left: a lead now"
    have = {t.lower(): t for t in tag_titles(rec)}
    lists = {str(x).strip().lower(): str(x) for x in (rec.get("lists") or []) if isinstance(x, str)}
    if action == "task":
        v = ctx.verdict(rec, "task", allow={"not_interested"})
        if not v.ok:
            return "held back: %s" % v.reason()
        if lanes.status_of(rec) != "not_interested":
            return "no longer a no: nothing to re-call"
        if not confirmed_number(rec):
            return "no confirmed number any more"
        t = it.get("task") or {}
        ctx.change(u, "task", None, t.get("title"))
        res = ctx.write("re-call task", researchroutes.create_task, ctx.crm, t.get("title") or "Call again", property_uuid=u,
                        user_uuid=t.get("user_uuid") or "", due=researchroutes.end_of_day(ctx.now, ctx.policy.tz),
                        notes=t.get("notes") or "")
        ledger.event(ctx.run_id, ctx.stage.id, u, KIND, dict(detail, task=str((res or {}).get("uuid") or ""), to=t.get("user")),
                     now=ctx.now, tz=ctx.policy.tz)
        return "re-call task written"
    drop = [have[t.lower()] for t in it.get("remove_tags") or [] if t.lower() in have]
    add = [t for t in it.get("add_tags") or [] if t.lower() not in have]
    off = [lists[t.lower()] for t in it.get("remove_lists") or [] if t.lower() in lists]
    if add:
        ctx.change(u, "add_tags", None, add)
        ctx.write("probate tags", ctx.crm.add_tags, u, add, rec)
        if not state.get("checked"):
            state["checked"] = True
            back = {t.lower() for t in tag_titles(ctx.crm.record(u))}
            if not {t.lower() for t in add} <= back:
                state["broken"] = True
                ctx.out.problem("the tag did not stick on %s (wrote %s); nothing more was written" % (it.get("street") or u[:8], add))
                return "failed"
    if drop:
        ctx.change(u, "remove_tags", drop, None)
        ctx.write("probate tags", ctx.crm.remove_tags, u, drop)
    for title in off:
        ctx.change(u, "remove_lists", [title], None)
        ctx.write("probate list", researchroutes.remove_list, ctx.crm, u, title)
    if it.get("note"):
        ctx.write("probate note", ctx.crm.message, u, it["note"])
    ledger.event(ctx.run_id, ctx.stage.id, u, KIND, detail, now=ctx.now, tz=ctx.policy.tz)
    return ACTION_WORDS.get(action, str(action))


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Approve a staged probate change list. Dry unless commit: a dry call only counts. `uploaded`
    is accepted so every batch kind is released the same way; it means nothing here."""
    from . import policy as policy_mod
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    items = b["items"]
    by = Counter(ACTION_WORDS.get(it.get("action"), it.get("action")) for it in items)
    if not commit:
        return {"batch": batch_id, "changes": len(items), "dry": True, "by_action": dict(by)}
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=True)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=True, out=out)
    state: dict = {}
    res: Counter = Counter()
    for it in items:
        if state.get("broken"):
            res["not written (stopped)"] += 1
            continue
        try:
            res[apply_one(ctx, it, state)] += 1
        except Exception as exc:  # noqa: BLE001  (say which record, keep going: each change stands alone)
            res["failed"] += 1
            out.problem("could not change %s (%s)" % (it.get("street") or it.get("record_uuid", "")[:8], str(exc)[:120]))
    ok = not state.get("broken")
    ledger.batch_set(batch_id, "sent" if ok else "failed", {"results": dict(res), "problems": out.problems})
    return {"batch": batch_id, "run": ctx.run_id, "results": dict(res), "problems": out.problems}
