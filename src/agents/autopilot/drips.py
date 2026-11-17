"""Long-term drips (build plan 2.6): a quiet lead always has a next step, and a reply ends it.

When a card lands in Ghosting, the board's sequence deletes every task and creates none. The lead
then has no next action at all, and nobody looks at it again. DataSift's own drip campaigns cannot
fill the gap: once started they cannot be stopped by a reply, and they send whenever their timer
says, inside our calling window or not. So the steps run on the brain's clock:

    ghosting   day 30 a text, day 60 an email, day 90 a task to review the calls and decide
    cold       day 30 a text, day 75 an email (the 45-day call task is the account's own)
    said no    day 60 a text, day 150 an email, for a lead who later said no (never a cold no)

Rules, each one a test:
  * A REPLY OR A STATUS CHANGE ENDS THE DRIP AT ONCE. A reply is an inbound text or a call of a
    minute or more, read from the shared log copy. The check runs when a step comes due and again
    when a person releases the batch, and an ended lead's staged steps are pulled.
  * HOT LEADS ARE NEVER DRIPPED, whatever the playbook lists. A hot lead is somebody's job today.
  * NOTHING IS SENT FROM HERE. A text or an email step is staged in a batch for a person. Release
    re-checks every item, writes the texts to a file for the texting tool and adds the tag that
    fires the email sequence. The review step is a task, created directly: it reaches no seller.
  * ONE STEP AT A TIME. A lead first seen on day 100 of its silence gets the first step now and
    the next one a month later, never three touches in one morning.
  * WHILE THE LOG IS BEHIND, NOTHING IS STAGED. A reply could be sitting in the part not yet read.

The day count starts when the record took its status (from the log copy). When the log does not
reach that far back it starts the day this stage first saw the record, which can only make a step
later, never earlier. Once a lead is on a drip its start is kept: the log copy forgets rows after
200 days, and a start that moved would run the whole drip a second time.
"""
from __future__ import annotations

import csv
import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, store
from . import activity, lanes, ledger
from . import leadclock
from . import leadroutes as routes
from .stages import Ctx, Summary, addr, assignee_of, status_of

log = logging.getLogger(__name__)

NEVER = ["Hot Lead"]
DO = ("text", "email", "task")
DEFAULT_DRIPS = {
    "ghosting": {
        "statuses": ["Ghosting Lead"],
        "steps": [
            {"day": 30, "do": "text",
             "copy": "Hi {first}, we talked about {street} a while back. Is selling still on your mind? This is {sender}."},
            {"day": 60, "do": "email", "tag": "Drip Email Ghosting"},
            {"day": 90, "do": "task", "title": "Review this ghosting lead's calls and decide: nurture or dead"},
        ],
    },
    "cold": {
        "statuses": ["Cold Lead", "follow_up"],
        "steps": [
            {"day": 30, "do": "text",
             "copy": "Hi {first}, checking in on {street}. Has anything changed on your end? This is {sender}."},
            {"day": 75, "do": "email", "tag": "Drip Email Cold"},
        ],
    },
    "said_no": {
        "statuses": ["not_interested"],
        "was_lead": True,
        "steps": [
            {"day": 60, "do": "text",
             "copy": "Hi {first}, no pressure at all. If selling {street} ever makes sense, I am glad to talk. This is {sender}."},
            {"day": 150, "do": "email", "tag": "Drip Email Said No"},
        ],
    },
}
# What a text to a seller never carries: the seller should feel found, not targeted, and a text
# that reads like a form gets a number flagged. A word is matched from its start, so "lien" does
# not catch "client" and "tax" still catches "taxes". The two dashes are named by number on
# purpose: the characters themselves never appear in this file.
BAD_WORDS = ("foreclos", "auction", "probate", "inherit", "tax", "lien", "violation", "evict", "divorce", "bankrupt",
             "obituar", "deceased", "estate")
BAD_PHRASES = ("behind on", "http", "www.", "$")
BAD_CHARS = (chr(8212), chr(8211), ";")        # the em dash and the en dash, by number


# ---------------------------------------------------------------------- the playbook's drips
def plans(stage) -> tuple[dict, list[str]]:
    """The drips this stage runs (the defaults, with the playbook's `drips:` laid over them) and what
    is wrong with them. A drip set to false is switched off; a step that cannot run is dropped and
    named, so one bad line does not stop the rest."""
    merged = json.loads(json.dumps(DEFAULT_DRIPS))
    for name, d in (stage.get("drips") or {}).items():
        if d is False or d is None:
            merged.pop(str(name), None)
        elif isinstance(d, dict):
            merged.setdefault(str(name), {}).update(d)
    out, bad = {}, []
    for name, d in merged.items():
        statuses = [str(s) for s in (d.get("statuses") or [])]
        steps = []
        last = -1.0
        for i, s in enumerate(d.get("steps") or [], 1):
            if not isinstance(s, dict) or s.get("do") not in DO or not isinstance(s.get("day"), (int, float)):
                bad.append("drip %s, step %d: needs a day and a do (%s)" % (name, i, ", ".join(DO)))
                continue
            if s["day"] <= last:
                bad.append("drip %s, step %d: its day must be later than the step before it" % (name, i))
                continue
            if s["do"] == "text":
                why = copy_problem(str(s.get("copy") or ""), int(stage.get("max_chars", 160)))
                if why:
                    bad.append("drip %s, step %d: the text %s; that step is skipped" % (name, i, why))
                    continue
            if s["do"] == "email" and not s.get("tag"):
                bad.append("drip %s, step %d: an email step needs the tag that fires its sequence" % (name, i))
                continue
            if s["do"] == "task" and not s.get("title"):
                bad.append("drip %s, step %d: a task step needs a title" % (name, i))
                continue
            last = float(s["day"])
            steps.append(dict(s))
        if not statuses or not steps:
            bad.append("drip %s has no status or no usable step; it is off" % name)
            continue
        out[name] = {"statuses": statuses, "steps": steps, "was_lead": bool(d.get("was_lead"))}
    return out, bad


def copy_problem(template: str, max_chars: int = 160) -> str:
    """Why a text template may not go to a seller ('' when it may). Judged with the street blanked,
    so a house on Estate Drive does not trip the word list."""
    if not template.strip():
        return "is empty"
    try:
        text = template.format(first="Pat", street="", sender="Sam")
        full = template.format(first="Pat", street="1234 Example Hollow Rd", sender="Samantha")
    except (KeyError, IndexError, ValueError):
        return "uses a placeholder other than {first}, {street} and {sender}"
    low = text.lower()
    words = [w for w in re.split(r"[^a-z]+", low) if w]
    for bad in BAD_WORDS:
        if any(w.startswith(bad) for w in words):
            return "names %r, which a seller text never does" % bad
    for bad in BAD_PHRASES:
        if bad in low:
            return "carries %r, which a seller text never does" % bad
    for ch in BAD_CHARS:
        if ch in text:
            return "carries a dash or a semicolon, which reads as a form letter"
    if text.count("?") > 1:
        return "asks more than one question"
    if len(full) > max_chars:
        return "runs past %d characters" % max_chars
    if any(w.isupper() and len(w) > 3 for w in text.split()):
        return "shouts a word in capitals"
    return ""


def first_name(rec: dict) -> str:
    """A first name fit to greet with, or 'there'. A company, an initial or a name with digits is
    not a name; greeting a trust by its first word reads like a list."""
    ow = (rec or {}).get("owner") or {}
    if ow.get("company"):
        return "there"
    tok = str(ow.get("first_name") or "").strip().split(" ")[0].strip(".,")
    if len(tok) < 2 or not tok.isalpha():
        return "there"
    return tok.capitalize() if (tok.isupper() or tok.islower()) else tok


def render(template: str, rec: dict, sender: str) -> str:
    """The text for one lead. With no sender name the sentence that names the sender is dropped:
    an unsigned text is fine, a guessed name is not."""
    t = template
    if not sender:
        parts = [p for p in t.replace("? ", "?|").replace(". ", ".|").split("|") if "{sender}" not in p]
        t = " ".join(parts)
    street = str(((rec or {}).get("address") or {}).get("street") or "").strip() or "your house"
    return " ".join(t.format(first=first_name(rec), street=street, sender=sender).split())


# ---------------------------------------------------------------------- where each lead stands
def _enrolled() -> list[tuple[str, dict]]:
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT record_uuid, value FROM ap_facts WHERE key='drip'").fetchall()
    out = []
    for r in rows:
        try:
            v = json.loads(r["value"])
        except ValueError:
            continue
        if isinstance(v, dict) and v.get("drip") and v.get("anchor"):
            out.append((r["record_uuid"], v))
    return out


def start_of(ctx: Ctx, u: str, status: str, plan: dict, name: str, fact) -> datetime | None:
    """When this lead's drip started: when the record took the status the drip runs on.

    A lead already on this drip keeps its start unless the log shows it took the status AGAIN since
    (left and came back). None when the drip is only for a lead who said no and the log does not
    show the record ever was a lead: a cold no belongs to the second-chance lane."""
    took = [e for e in activity.events("property.status.updated", resource=u)
            if str(e["data"].get("to") or "").strip().lower() == status]
    if plan.get("was_lead"):
        took = [e for e in took if str(e["data"].get("from") or "").strip().lower() in lanes.LEAD_STATUSES]
    newest = took[-1]["when"] if took else None
    if isinstance(fact, dict) and fact.get("drip") == name and fact.get("status") == status and fact.get("anchor"):
        kept = datetime.fromisoformat(fact["anchor"])
        if newest is None or newest <= kept + timedelta(seconds=2):
            return kept
        return newest
    if newest is not None:
        return newest
    if plan.get("was_lead"):
        return None
    return ledger.first_seen(u, "drip:%s" % name, ctx.now)


def done_steps(u: str, name: str, anchor: str) -> dict:
    """step number -> when it was done (or passed over), for this run of the drip. From the ledger,
    so only what a live run really did counts."""
    out = {}
    for e in ledger.events(u, "drip"):
        d = e["detail"]
        if d.get("drip") == name and d.get("anchor") == anchor and d.get("step") is not None:
            out[int(d["step"])] = datetime.fromisoformat(e["at"])
    return out


def next_step(plan: dict, done: dict, anchor: datetime) -> tuple[int, dict, datetime] | None:
    """(step number, the step, when it is due): the first step not yet done. A step is due on its
    day, and never sooner after the step before it than the gap the plan put between them."""
    steps = plan["steps"]
    for i, s in enumerate(steps):
        if i in done:
            continue
        due = anchor + timedelta(days=float(s["day"]))
        if i > 0 and (i - 1) in done:
            due = max(due, done[i - 1] + timedelta(days=float(s["day"]) - float(steps[i - 1]["day"])))
        return i, s, due
    return None


def _staged(stage_id: str) -> dict:
    """(record, drip, step) -> batch id, for every drip item waiting on a person."""
    out = {}
    for b in ledger.batches(status="staged", stage=stage_id):
        full = ledger.batch(b["id"])
        if not full or full["kind"] != "drip":
            continue
        for it in full["items"]:
            out[(it.get("record_uuid"), it.get("drip"), it.get("step"))] = b["id"]
    return out


def pull(ctx: Ctx, ended: dict) -> int:
    """Take an ended lead's steps out of every staged drip batch. Returns how many were pulled."""
    pulled = 0
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        full = ledger.batch(b["id"])
        if not full or full["kind"] != "drip":
            continue
        keep = [it for it in full["items"] if it.get("record_uuid") not in ended]
        gone = len(full["items"]) - len(keep)
        if gone:
            pulled += gone
            if ctx.live:
                ledger.batch_items_set(b["id"], keep)
    return pulled


def scheduled(pol, record_uuid: str, now: datetime) -> tuple[datetime, str] | None:
    """(when, what) of the next drip step waiting for a record, or None. The next-action check asks
    this: a ghosting lead with a step ahead of it has a next action even with no open task."""
    fact = ledger.fact(record_uuid, "drip")
    if not isinstance(fact, dict) or fact.get("ended") or not fact.get("anchor"):
        return None
    for st in pol.stages.values():
        if st.kind != "drips" or st.mode == "off":
            continue
        plan = plans(st)[0].get(fact.get("drip") or "")
        if not plan:
            continue
        nxt = next_step(plan, done_steps(record_uuid, fact["drip"], fact["anchor"]), datetime.fromisoformat(fact["anchor"]))
        if nxt:
            return nxt[2], nxt[1]["do"]
    return None


def _reply(owner_uuid: str, u: str, anchor: datetime, seconds: int) -> str:
    when, how = leadclock.last_conversation(owner_uuid, u, since=anchor, seconds=seconds, notes=False)
    if not when:
        return ""
    return "replied (a %s on %s)" % (how, when.strftime("%b %d").replace(" 0", " "))


def _sender(ctx: Ctx, rec: dict, names: dict) -> str:
    fixed = str(ctx.stage.get("sender_name") or "").strip()
    return fixed or routes.person_name(ctx.stage, assignee_of(rec), names)


def _never(stage) -> set:
    return {str(s).strip().lower() for s in (stage.get("never") or [])} | {s.lower() for s in NEVER}


def _rows(ctx: Ctx, plans_: dict, by_status: dict, stored: list[str], cap: int) -> tuple[dict, bool]:
    """The records each drip could run on. A drip on an ordinary status is one search. A drip for
    leads who said no is NOT a search for every not-interested record in the account (thousands of
    cold nos): the log copy names the records that went from a lead status to that one, and only
    those are read."""
    plain = [s for s in stored if not plans_[by_status[s.strip().lower()]]["was_lead"]]
    rows = {r["uuid"]: r for r in ctx.crm.rows({"any_property_status": plain}, cap=cap)} if plain else {}
    whole = len(rows) < cap
    former: dict[str, str] = {}
    wanted = {s.strip().lower(): s for s in stored if plans_[by_status[s.strip().lower()]]["was_lead"]}
    if wanted:
        for e in activity.events("property.status.updated"):
            to = str(e["data"].get("to") or "").strip().lower()
            if to in wanted and str(e["data"].get("from") or "").strip().lower() in lanes.LEAD_STATUSES and e["resource"]:
                former[e["resource"]] = wanted[to]
    if former:
        ids = sorted(former)[:cap]
        got = ctx.crm.rows({"properties": ids, "any_property_status": sorted(set(former.values()))}, cap=cap)
        for r in got:
            if r.get("uuid") in former:           # the filter is checked here, never trusted
                rows[r["uuid"]] = r
    return rows, whole


def _pass_over(ctx: Ctx, u: str, base: dict, why: str) -> None:
    """Mark a step as passed over, so the next one comes on its own day. Remembered only by a live
    run, like every step: a plan run that skipped it would hide it from the first live one."""
    ctx.out.tally["steps passed over: %s" % why] += 1
    if ctx.live:
        ledger.event(ctx.run_id, ctx.stage.id, u, "drip", dict(base, skipped=why), now=ctx.now, tz=ctx.policy.tz)


# ---------------------------------------------------------------------- the stage
def run_drips(ctx: Ctx) -> None:
    st = ctx.stage
    tz = ctx.policy.tz
    conv = int(st.get("conversation_seconds", 60))
    plans_, bad = plans(st)
    for b in bad:
        ctx.out.problem(b)
    never = _never(st)
    by_status: dict[str, str] = {}
    stored: list[str] = []
    for name, d in plans_.items():
        for s in d["statuses"]:
            if s.strip().lower() in never:
                ctx.out.problem("the %s drip lists %s, and that status is never dripped; it was left out" % (name, s))
                continue
            if s.strip().lower() not in by_status:
                by_status[s.strip().lower()] = name
                stored.append(s)
    if not by_status:
        ctx.out.head = "No drip is set up."
        return
    synced = leadclock.sync_logs(ctx, ("state", "calls", "texts"))
    rows, whole = _rows(ctx, plans_, by_status, stored, int(st.get("max_scan", 2000)))
    ctx.out.found = len(rows)

    # ---- 1. who is on a drip, and whose drip just ended
    for u, row in rows.items():
        status = status_of(row)
        name = by_status.get(status)
        if not name:
            continue
        fact = ledger.fact(u, "drip")
        start = start_of(ctx, u, status, plans_[name], name, fact)
        if start is None:
            ctx.out.tally["said no without ever being a lead (the second-chance lane's, not dripped)"] += 1
            continue
        stamp = start.isoformat(timespec="seconds")
        if not isinstance(fact, dict) or fact.get("drip") != name or fact.get("anchor") != stamp or fact.get("status") != status:
            ledger.set_fact(u, "drip", {"drip": name, "status": status, "anchor": stamp, "ended": "",
                                        "owner": (row.get("owner") or {}).get("uuid") or "", "street": addr(row)})
    ended: dict[str, str] = {}
    on = 0
    for u, fact in _enrolled():
        if fact.get("ended"):
            continue
        row = rows.get(u)
        why = ""
        if row is None or status_of(row) != fact.get("status"):
            if whole or row is not None:
                why = "status changed%s" % ((" to %s" % (status_of(row) or "none")) if row is not None else "")
        elif synced:
            why = _reply(fact.get("owner") or "", u, datetime.fromisoformat(fact["anchor"]), conv)
        if why:
            ended[u] = why
            ledger.set_fact(u, "drip", dict(fact, ended=why))     # knowledge: this lead is no longer on a drip
            if why.startswith("replied"):
                ctx.out.line("%s: %s, drip ended" % (fact.get("street") or u[:8], why))
        else:
            on += 1
    replied = [u for u, w in ended.items() if w.startswith("replied")]
    if replied:
        ctx.out.problem("%d lead(s) answered while on a drip: call them today (%s)" % (
            len(replied), ", ".join((ledger.fact(u, "drip") or {}).get("street") or u[:8] for u in replied[:3])))
    pulled = pull(ctx, ended) if ended else 0
    if pulled:
        ctx.out.tally["staged steps pulled because the drip ended"] = pulled
    if ended:
        ctx.out.tally["drips ended (a reply or a status change)"] = len(ended)
    if not synced:
        ctx.out.head = "%d lead(s) are on a drip. The log is behind, so no step was staged this run." % on
        return

    # ---- 2. what is due
    staged = _staged(st.id)
    due = []
    for u, fact in _enrolled():
        if fact.get("ended") or u not in rows or fact.get("drip") not in plans_:
            continue
        nxt = next_step(plans_[fact["drip"]], done_steps(u, fact["drip"], fact["anchor"]), datetime.fromisoformat(fact["anchor"]))
        if nxt is None or nxt[2] > ctx.now:
            continue
        if (u, fact["drip"], nxt[0]) in staged:
            ctx.out.tally["already staged, waiting on release"] += 1
            continue
        due.append((nxt[2], u, fact, nxt[0], nxt[1]))
    due.sort(key=lambda x: x[0])

    # ---- 3. stage the texts and emails, create the review tasks
    names = routes.names_from_log(ctx.now - timedelta(days=90))
    items, tasks_made, reads = [], 0, 0
    cap_n, max_reads = int(st.get("daily_items", 25)), int(st.get("max_reads", 60))
    for _at, u, fact, idx, step in due:
        if len(items) + tasks_made >= cap_n:
            ctx.out.tally["left for the next run (cap %d a run)" % cap_n] += 1
            continue
        if reads >= max_reads:
            ctx.out.tally["not read this run (read cap)"] += 1
            continue
        reads += 1
        row = rows[u]
        rec = ctx.crm.record(u)
        if status_of(rec) in never:
            ctx.out.tally["hot leads never dripped"] += 1
            continue
        do = step["do"]
        # An email has no channel of its own in the gate: it is judged as a task, which every
        # blanket rule covers (Do Not Market, a litigator, an opt-out, a sale, a death, Mail Only).
        v = ctx.verdict(rec, "text" if do == "text" else "task", row=row)
        base = {"record_uuid": u, "owner_uuid": (rec.get("owner") or {}).get("uuid") or "", "street": addr(rec),
                "drip": fact["drip"], "step": idx, "do": do, "anchor": fact["anchor"], "status": fact["status"]}
        day = "day %d" % int(step["day"])
        if not v.ok:
            if do == "text" and v.code == "no_number":
                # No number a text may use is not a reason to stop the drip: waiting will not grow
                # one, and the email and the review task behind this step still have to come.
                _pass_over(ctx, u, base, "no number a text may use")
            else:
                ctx.out.tally["held back: %s" % v.reason()] += 1
            continue
        if do == "task":
            ctx.change(u, "task_create", None, {"title": step["title"]})
            r = ctx.write("drip task", routes.create_task, ctx.crm, u, step["title"],
                          routes.end_of_day(routes.business_day(ctx.now, tz, int(step.get("due_days", 2))), tz),
                          routes.task_owner(st, rec),
                          "On the %s drip since %s. Listen to the calls and decide." % (
                              fact["drip"].replace("_", " "), routes.nice_day(datetime.fromisoformat(fact["anchor"]), tz)))
            if ctx.live:
                ledger.event(ctx.run_id, st.id, u, "drip", dict(base, task=routes.task_id(r)), now=ctx.now, tz=tz)
            tasks_made += 1
            ctx.out.line("%s: %s, a task to review the calls" % (addr(rec), day))
        elif do == "text":
            pick = sorted(v.numbers, key=lambda n: n.get("status") != "CORRECT")[0]
            items.append(dict(base, to=pick["number"], first=first_name(rec),
                              body=render(step["copy"], rec, _sender(ctx, rec, names))))
            ctx.out.line("%s: %s, a text to the number ending %s" % (addr(rec), day, pick["number"][-4:]))
        else:
            if not ctx.crm.tag_uuid(step["tag"]):
                ctx.out.problem("there is no tag %r in this account, so the email step is passed over. Build the email "
                                "sequence that fires on that tag in Sequence Builder to switch it on" % step["tag"])
                _pass_over(ctx, u, base, "no email sequence yet")
                continue
            ow = rec.get("owner") or {}
            if not (ow.get("emails") or ow.get("primary_email") or ow.get("email")):
                _pass_over(ctx, u, base, "no email address on the owner")
                continue
            items.append(dict(base, tag=step["tag"]))
            ctx.out.line("%s: %s, an email (tag %s)" % (addr(rec), day, step["tag"]))
    n_text = sum(1 for it in items if it["do"] == "text")
    n_mail = len(items) - n_text
    if items and not leadclock.texts_reach_the_log(ctx.now):
        # The promise is that a reply ends the drip. A reply by text can only do that if the texting
        # tool writes replies to DataSift's log. When none ever has, say so beside the batch.
        ctx.out.problem("no inbound text has reached the log in %d days. If the texting tool does not write replies to "
                        "DataSift, a reply by text will not end a drip here: check each thread before releasing the batch"
                        % leadclock.TEXT_PROOF_DAYS)
    parts = [p for p in ("%d text(s)" % n_text if n_text else "", "%d email(s)" % n_mail if n_mail else "") if p]
    what = " and ".join(parts)
    made = ("%d review task(s)" % tasks_made) if tasks_made else ""
    ctx.out.acted = len(items) + tasks_made
    tail = " %d lead(s) are on a drip." % on
    if not items and not tasks_made:
        ctx.out.head = ("%d lead(s) are on a drip; nothing is due." % on) if on else "No lead is on a drip."
        return
    if not ctx.live:
        todo = [p for p in (("stage %s" % what) if what else "", ("create %s" % made) if made else "") if p]
        ctx.out.head = "Would %s.%s" % (" and ".join(todo), tail)
        return
    if items:
        bid = ledger.batch_create(st.id, "drip", items, 0)
        ctx.out.batch = bid
        ctx.out.head = ("Staged batch %d: %s for a person to release (python src/agents/cli.py autopilot release %d --commit)%s.%s"
                        % (bid, what, bid, (", and created %s" % made) if made else "", tail))
    else:
        ctx.out.head = "Created %s.%s" % (made, tail)


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, now: datetime | None = None) -> dict:
    """Release a staged drip batch. Every item is checked again first: the lead must still hold the
    status, must not have answered since, and must still pass the gate. The texts that pass are
    written to one file for the texting tool (this code sends nothing); an email step adds the tag
    that fires its sequence. Dry unless commit."""
    from . import policy as policy_mod
    b = ledger.batch(batch_id)
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    if b["kind"] != "drip":
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    items = b["items"]
    by = Counter(it.get("do") for it in items)
    if not commit:
        return {"batch": batch_id, "texts": by.get("text", 0), "emails": by.get("email", 0), "dry": True}
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    if crm is None:
        from .crm import CRM
        crm = CRM()
    out = Summary(stage=st.id, kind="drips", owner=st.owner, mode=st.mode, live=True)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="release-%d" % batch_id, now=now or datetime.now(timezone.utc),
              live=True, out=out)
    if not leadclock.sync_logs(ctx, ("state", "calls", "texts")):
        return {"batch": batch_id, "texts": 0, "emails": 0, "problems": out.problems + [
            "nothing was released: the log is behind, and a reply could be in the part not read yet"]}
    plans_, _bad = plans(st)
    conv = int(st.get("conversation_seconds", 60))
    never = _never(st)
    texts, held = [], Counter()
    emails = 0
    for it in items:
        u = it.get("record_uuid") or ""
        rec = crm.record(u)
        status = status_of(rec)
        plan = plans_.get(it.get("drip") or "")
        fact = ledger.fact(u, "drip") or {}
        if status in never:
            held["now a hot lead"] += 1
            continue
        if not plan or status != it.get("status") or fact.get("anchor") != it.get("anchor") or fact.get("ended"):
            held["the status changed"] += 1
            continue
        owner = (rec.get("owner") or {}).get("uuid") or ""
        if _reply(owner, u, datetime.fromisoformat(it["anchor"]), conv):
            held["the lead answered"] += 1
            ledger.set_fact(u, "drip", dict(fact, ended="replied"))
            continue
        v = ctx.verdict(rec, "text" if it.get("do") == "text" else "task")
        if not v.ok:
            held[v.reason()] += 1
            continue
        if it.get("do") == "text":
            if it.get("to") not in {n["number"] for n in v.numbers}:
                held["the number is no longer usable"] += 1
                continue
            texts.append(it)
        else:
            ctx.change(u, "add_tags", None, [it["tag"]])
            ctx.write("drip email", crm.add_tags, u, [it["tag"]], rec)
            emails += 1
        ledger.event(ctx.run_id, st.id, u, "drip", {k: it.get(k) for k in ("drip", "step", "do", "anchor")} | {"batch": batch_id},
                     now=ctx.now, tz=pol.tz)
    path = ""
    if texts:
        folder = Path(config.DATA_DIR) / "drips"
        folder.mkdir(parents=True, exist_ok=True)
        path = str(folder / ("batch_%d.csv" % batch_id))
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["phone", "first_name", "street", "message", "record_uuid", "drip", "step"])
            for it in texts:
                w.writerow([it["to"], it.get("first") or "", it.get("street") or "", it["body"], it["record_uuid"], it["drip"],
                            int(it["step"]) + 1])
    ledger.batch_set(batch_id, "sent", {"texts": len(texts), "emails": emails, "held": dict(held), "file": path})
    return {"batch": batch_id, "texts": len(texts), "emails": emails, "held": dict(held), "file": path,
            "next": ("load %s into the texting tool" % path) if path else "", "problems": out.problems}
