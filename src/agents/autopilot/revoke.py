"""Revocation (build plan 0.17): a no stops what is already in motion.

The gate checks a record when it is picked up and again when a batch is released. Between those
two moments an owner can opt out, die, sell or become a lead, and work already queued for them
keeps going unless something pulls it. This stage is that something. It reads the day's status and
tag changes from the shared log, finds the owners who just became untouchable, and takes them out
of what is still waiting:

  * staged batches (the autopilot's own mail, texts and drips): the owner's records are removed
    before anyone releases the batch;
  * Smarter Contact: every phone on the owner's record is marked do-not-contact there, so a
    campaign that is still sending skips them. A new LEAD is not marked do-not-contact (the lead
    manager still texts them one to one); the stage only says which leads still have numbers in
    Smarter Contact.

The Smarter Contact login lives on the texting app, not on the brain (2026-11-19). So on the brain
this half rides the texting app's load route (scpush.revoke: AGENTS_SC_LOAD_URL and
AGENTS_SC_LOAD_TOKEN, the same two settings the text loads use), and the texting app does the work
with its own login (sms_agent.sc_load.revoke_run). A box that holds the login itself does the same
work in process. Either way the answer comes back number by number: marked, already on Smarter
Contact's list, not there, a new lead's number left alone, or failed. Every owner sent is written
to the ledger with that answer, and an owner whose numbers are not all settled stays in a queue for
the next run, so a pull that failed is tried again and never dropped. A plan run looks the numbers
up (the texting app's dry run) and marks nothing.

It works by OWNER, not by phone. Five of nine measured replies came from a different number than
the one that was texted, so pulling only the number that answered leaves the others queued.

The texting app's own queue lives in another app and is pulled there, by the same gate, right
before each send.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .. import store
from . import activity, budget, gate, ledger, scpush
from .stages import Ctx, addr, tag_titles  # noqa: F401  (tag_titles kept for older imports)

log = logging.getLogger(__name__)

# Two high-water marks, on purpose. A plan run reports each change once and moves only the PLAN
# mark. The LIVE mark moves only when something was really pulled, so the first live run starts
# from its own window and does not skip the owners that plan runs merely listed.
KV = "autopilot:revoke:through"
KV_PLAN = "autopilot:revoke:plan_through"

# The Smarter Contact half keeps its own memory, live runs only. A plan run keeps none: it lists
# what it found, and the first live run looks back first_window_hours on its own.
SC_QUEUE = "autopilot:revoke:sc_queue"      # record uuid -> {why, kind, since, tries, last}
SC_JOBS = "autopilot:revoke:sc_jobs"        # jobs the texting app was still working on when a run stopped waiting
SC_LAST_ID = "autopilot:revoke:sc_last_id"  # the last job number handed out
# Outcomes that settle a number. would_mark (a dry run's answer) and failed do not.
SETTLED = {"marked", "already", "not_in_sc", "lead", "bad_number"}
JOB_STALE = timedelta(hours=3)              # a job still "loading" after this is given up on and sent again
TRIES_SAID = 3                              # an owner still queued after this many tries is named every run


# The reasons a record becomes untouchable, as the stage's `sc_reasons` setting names them. Every
# reason pulls the record out of staged batches. Only the reasons sc_reasons lists reach Smarter
# Contact, where a do-not-contact mark is permanent (2026-11-19: kept to the plain do-not-contact
# signals on prod-account, since a sale, a Not Interested or a possible death can be wrong or temporary).
SC_REASON_KEYS = ("opt_out", "do_not_market", "litigator", "sold", "finished", "deceased", "lead")


def _suppressing(e: dict, cfg: gate.GateConfig) -> tuple[str, str]:
    """(why, kind) when this logged change makes a record untouchable, else ("", "")."""
    why, kind, _ = _suppressing_key(e, cfg)
    return why, kind


def _suppressing_key(e: dict, cfg: gate.GateConfig) -> tuple[str, str, str]:
    """(why, kind, reason key) when this logged change makes a record untouchable, else ("", "", "").
    The key is one of SC_REASON_KEYS."""
    et, d = e["event_type"], e["data"]
    if et == "property.status.updated":
        to = str(d.get("to") or "").strip().lower()
        code, _ = gate._status_rule(to, cfg)
        if to in ("dnc", "opt_out"):
            return "the owner opted out", "stop", "opt_out"
        if code == "sold":
            return "the house sold", "stop", "sold"
        if code == "lead":
            return "became a lead (%s)" % to, "lead", "lead"
        if code == "finished":
            return "status %s" % to, "stop", "finished"
        return "", "", ""
    if et == "property.tags.added":
        tags = {str(t).lower() for t in d.get("tags") or []}
        if tags & {t.lower() for t in cfg.do_not_market_tags}:
            return "tagged Do Not Market", "stop", "do_not_market"
        if tags & {t.lower() for t in cfg.litigator_tags}:
            return "a litigator's number", "stop", "litigator"
        if tags & {t.lower() for t in cfg.sold_tags}:
            return "the house sold", "stop", "sold"
        if tags & {t.lower() for t in cfg.deceased_tags}:
            return "the owner may have died", "stop", "deceased"
    return "", "", ""


def _sc_reasons(ctx: Ctx) -> set[str] | None:
    """The reason keys that reach Smarter Contact, or None for every reason (the default, as it was
    before the setting existed). An unknown key is named and ignored."""
    raw = ctx.stage.get("sc_reasons")
    if raw is None:
        return None
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    keep: set[str] = set()
    for v in vals:
        k = str(v or "").strip().lower()
        if k in SC_REASON_KEYS:
            keep.add(k)
        elif k:
            ctx.out.problem("sc_reasons: %r is not a reason (use %s); it is ignored" % (v, ", ".join(SC_REASON_KEYS)))
    return keep


def pull_from_batches(ctx: Ctx, record_uuids: set[str], why: dict) -> int:
    """Take these records out of every staged batch: mail pieces, texts and lead drip steps. Returns
    how many were pulled. Until 2026-11-13 only mail was pulled, so a record that became untouchable
    stayed in a staged text batch, held back only by the gate at release and missing from this count."""
    pulled = 0
    for b in ledger.batches(status="staged"):
        full = ledger.batch(b["id"])
        if not full or full["kind"] not in ("mail", "text", "drip", "heir_mail", "heir_text", "obituary_mail"):
            continue
        hit = [it for it in full["items"] if it.get("record_uuid") in record_uuids]
        if not hit:
            continue
        if full["kind"] == "text":
            from . import textlane
            gone, what = textlane.pull(b["id"], record_uuids, commit=ctx.live), "text(s)"
        else:
            keep = [it for it in full["items"] if it.get("record_uuid") not in record_uuids]
            gone = len(hit)
            what = {"mail": "piece(s)", "drip": "drip step(s)", "heir_mail": "heir letter(s)",
                    "heir_text": "heir text(s)", "obituary_mail": "obituary letter(s)"}[full["kind"]]
            if ctx.live:
                if full["kind"] == "mail":
                    ledger.batch_items_set(b["id"], keep, sum(float(it.get("price") or 0) for it in keep))
                else:
                    ledger.batch_items_set(b["id"], keep)
                if full["kind"] == "heir_text":
                    from . import limits
                    for u in {it.get("record_uuid") for it in hit}:
                        limits.drop_text(b["id"], u)
        pulled += gone
        ctx.out.line("batch %d: %d %s pulled before release" % (b["id"], gone, what))
        if ctx.live:
            for it in hit:
                ledger.event(ctx.run_id, ctx.stage.id, it["record_uuid"], "revoke",
                             {"from": "%s batch %d" % (full["kind"], b["id"]), "why": why.get(it["record_uuid"], "")},
                             now=ctx.now, tz=ctx.policy.tz)
    return pulled


# ---------------------------------------------------------------------- Smarter Contact
def _direct():
    """Smarter Contact's own client when this box holds the login, else None. The texting app holds
    it; the brain does not."""
    try:
        from sms_agent import sc_client
        return sc_client if sc_client.configured() else None
    except Exception:  # noqa: BLE001  (no texting package or no login: the other route is tried)
        return None


def _route() -> tuple[str, object]:
    """("this box", client) when this box holds the login, ("texting app", None) when it can hand
    the job to the texting app, ("", None) when it can do neither."""
    sc = _direct()
    if sc is not None:
        return "this box", sc
    if scpush.ready():
        return "texting app", None
    return "", None


def _kv_json(key: str, empty):
    raw = store.kv_get(key)
    if not raw:
        return empty
    try:
        value = json.loads(raw)
    except ValueError:
        return empty
    return value if isinstance(value, type(empty)) else empty


def queued() -> dict:
    """The owners whose Smarter Contact pull is not settled yet: record uuid -> {why, kind, since,
    tries, last}. Live runs only."""
    return _kv_json(SC_QUEUE, {})


def waiting_jobs() -> list:
    """Jobs the texting app was still working on when a live run stopped waiting for them."""
    return _kv_json(SC_JOBS, [])


def _next_id() -> int:
    """A job number. Milliseconds since 1970, so it can never be a text batch's number on the same
    route (the texting app refuses one below a billion) and does not repeat after this box's
    database is replaced; never lower than the last one handed out."""
    try:
        last = int(store.kv_get(SC_LAST_ID) or 0)
    except ValueError:
        last = 0
    n = max(int(time.time() * 1000), last + 1)
    store.kv_set(SC_LAST_ID, str(n))
    return n


def _answers(doc: dict, job: dict) -> bool:
    """True when the texting app's document is this job's own (its action and its id)."""
    return doc.get("action") == "revoke" and str(doc.get("job") or "") == str(job.get("job") or "")


def _bump(queue: dict, u: str, last: str) -> None:
    q = queue.get(u)
    if q is not None:
        q["tries"] = int(q.get("tries") or 0) + 1
        q["last"] = last


def _owner_words(rows: list, live: bool) -> str:
    c = Counter(r.get("outcome") for r in rows)
    parts = []
    if c["marked"]:
        parts.append("%d marked do-not-contact" % c["marked"])
    if c["would_mark"]:
        parts.append("%d would be marked do-not-contact" % c["would_mark"])
    if c["already"]:
        parts.append("%d already on its do-not-text list" % c["already"])
    if c["lead"]:
        parts.append("%d left for a person (a new lead)" % c["lead"])
    if c["not_in_sc"]:
        parts.append("%d not there" % c["not_in_sc"])
    if c["bad_number"]:
        parts.append("%d not a ten-digit number" % c["bad_number"])
    if c["failed"]:
        parts.append("%d failed%s" % (c["failed"], ", tried again next run" if live else ""))
    return ", ".join(parts) or "no answer for its numbers"


def _settle(ctx: Ctx, queue: dict, job: dict, doc: dict, res: Counter) -> None:
    """Take one finished job's answer: count each number's outcome, write each owner's numbers to
    the ledger (live), and keep in the queue every owner whose numbers are not all settled."""
    result = doc.get("result") or {}
    records = [str(u) for u in job.get("records") or []]
    if result.get("refused"):
        ctx.out.problem("Smarter Contact could not take the do-not-contact job: %s" % result["refused"])
        for u in records:
            _bump(queue, u, "refused: %s" % str(result["refused"])[:80])
        res["not checked"] += sum(int(n) for n in (job.get("numbers") or {}).values())
        return
    if ctx.live and result.get("dry"):
        ctx.out.problem("the texting app ran the do-not-contact job as a dry run (SMS_AGENT_DRY_RUN is on there), "
                        "so nothing was marked and its owners stay queued")
        for u in records:
            _bump(queue, u, "ran dry")
        res["not checked"] += sum(int(n) for n in (job.get("numbers") or {}).values())
        return
    for p in result.get("problems") or []:
        ctx.out.problem("Smarter Contact: %s" % p)
    by: dict[str, list] = {}
    for r in result.get("numbers") or []:
        by.setdefault(str(r.get("record") or ""), []).append(r)
    where = job.get("where") or {}
    for u in records:
        rows = by.get(u) or []
        for r in rows:
            res[str(r.get("outcome") or "failed")] += 1
        q = queue.get(u) or {}
        if ctx.live:
            ledger.event(ctx.run_id, ctx.stage.id, u, "revoke",
                         {"from": "smarter contact", "why": q.get("why", ""), "via": job.get("via", ""),
                          "job": job.get("batch"),
                          "numbers": [{"number": r.get("number"), "outcome": r.get("outcome"),
                                       "detail": r.get("detail", "")} for r in rows]},
                         now=ctx.now, tz=ctx.policy.tz)
        if q.get("kind") != "lead":
            ctx.out.line("%s: %s; Smarter Contact: %s" % (where.get(u) or "a record", q.get("why") or "untouchable",
                                                         _owner_words(rows, ctx.live)))
        if rows and all(r.get("outcome") in SETTLED for r in rows):
            queue.pop(u, None)
        else:
            _bump(queue, u, "a number was not settled")


def _send(ctx: Ctx, route: str, sc, job: dict) -> dict:
    """Run the job where the login is. Returns a status document shaped like the texting app's."""
    if route == "this box":
        from sms_agent import sc_load
        try:
            result = sc_load.revoke_run(job, commit=ctx.live, sc=sc)
        except Exception as exc:  # noqa: BLE001
            return {"status": "not handed over", "why": "Smarter Contact could not be reached from this box (%s)" % str(exc)[:120]}
        return {"status": "done" if result.get("ok") else "failed", "action": "revoke", "batch": job["batch"],
                "job": job["job"], "result": result}
    return scpush.revoke(job, dry=not ctx.live,
                         wait_s=float(ctx.stage.get("sc_wait_seconds", scpush.REVOKE_WAIT_S)))


def pull_from_smarter_contact(ctx: Ctx, why: dict, kinds: dict) -> Counter:
    """Take the owners who just became untouchable, and any an earlier run could not finish, out of
    Smarter Contact. Returns a tally of what happened to their numbers (by outcome)."""
    st = ctx.stage
    res: Counter = Counter()
    if not bool(st.get("smarter_contact", True)):
        return res
    live = ctx.live
    stamp = ctx.now.isoformat(timespec="seconds")
    queue = queued() if live else {}
    for u, reason in why.items():
        q = queue.setdefault(u, {"since": stamp, "tries": 0})
        kind = kinds.get(u) or "stop"
        q["kind"] = "stop" if "stop" in (kind, q.get("kind")) else kind
        q["why"] = reason
    jobs = waiting_jobs() if live else []
    if not queue and not jobs:
        return res
    route, sc = _route()
    if not route:
        ctx.out.tally["Smarter Contact not reachable from this box (its campaigns were not checked)"] = 1
        if live:
            store.kv_set(SC_QUEUE, json.dumps(queue))
            ctx.out.problem("could not reach Smarter Contact: this box has neither its login nor the texting app's load "
                            "route (AGENTS_SC_LOAD_URL and AGENTS_SC_LOAD_TOKEN), so %d owner(s) wait to be taken out "
                            "of its campaigns" % len(queue))
        return res

    # A job an earlier run stopped waiting on: read its answer before sending anyone again.
    in_flight: set[str] = set()
    keep = []
    for job in jobs:
        doc = scpush.job_status(job.get("batch") or 0) if scpush.ready() else {"status": "loading"}
        if doc.get("status") in ("done", "failed") and _answers(doc, job):
            _settle(ctx, queue, job, doc, res)
            continue
        try:
            age = ctx.now - datetime.fromisoformat(str(job.get("sent")))
        except (TypeError, ValueError):
            age = JOB_STALE
        if doc.get("status") in ("loading", "unknown") and age < JOB_STALE:
            keep.append(job)
            in_flight.update(job.get("records") or [])
            continue
        ctx.out.problem("the texting app never finished Smarter Contact job %s; its %d owner(s) go out again in a new job"
                        % (job.get("batch"), len(job.get("records") or [])))
        for u in job.get("records") or []:
            _bump(queue, u, "job %s never finished" % job.get("batch"))
    jobs = keep

    order = sorted((u for u in queue if u not in in_flight),
                   key=lambda u: (int(queue[u].get("tries") or 0), str(queue[u].get("since") or ""), u))
    cap = max(1, int(st.get("max_records", 80)))
    picked = order[:cap]
    if len(order) > cap:
        res["waiting for the next run"] += len(order) - cap
    owners, where = [], {}
    for i, u in enumerate(picked):
        try:
            rec = ctx.crm.record(u) or {}
        except budget.BudgetExhausted:
            ctx.out.problem("stopped reading records for Smarter Contact: this hour's request budget is used up, so "
                            "%d owner(s) wait for the next run" % (len(picked) - i))
            res["waiting for the next run"] += len(picked) - i
            break
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "code", None) == 404:
                queue.pop(u, None)
                res["record gone from DataSift"] += 1
                ctx.out.problem("a record that just became untouchable is gone from DataSift, so its numbers could not be "
                                "taken out of Smarter Contact")
                continue
            _bump(queue, u, "could not read the record")
            ctx.out.problem("could not read a record to take its numbers out of Smarter Contact (%s)" % str(exc)[:80])
            continue
        numbers: list[str] = []
        for p in (rec.get("owner") or {}).get("phones") or []:
            n = gate.digits(p.get("number"))
            if n and n not in numbers:
                numbers.append(n)
        if not numbers:
            queue.pop(u, None)
            res["record with no number"] += 1
            continue
        where[u] = addr(rec)
        owners.append({"record": u, "dnc": queue[u].get("kind") != "lead", "numbers": numbers})

    if owners:
        job = {"action": "revoke", "batch": _next_id(), "job": uuid.uuid4().hex, "owners": owners}
        info = {"batch": job["batch"], "job": job["job"], "sent": stamp, "via": route, "where": where,
                "records": [o["record"] for o in owners],
                "numbers": {o["record"]: len(o["numbers"]) for o in owners}}
        total = sum(len(o["numbers"]) for o in owners)
        doc = _send(ctx, route, sc, job)
        status = doc.get("status")
        if status in ("done", "failed") and _answers(doc, job):
            _settle(ctx, queue, info, doc, res)
        elif status == "not handed over" or status in ("done", "failed"):
            why_not = str(doc.get("why") or "the answer was for another job")
            if "names no group" in why_not:
                why_not += "; the texting app does not take do-not-contact jobs yet (it needs the 2026-11-19 change deployed)"
            ctx.out.problem(("could not hand the do-not-contact job to the texting app: %s" if route == "texting app" else
                             "could not run the do-not-contact job on this box: %s") % why_not)
            for u in info["records"]:
                _bump(queue, u, "not handed over")
            res["not checked"] += total
        elif live:
            jobs.append(info)
            res["still being worked on by the texting app"] += total
            ctx.out.line("Smarter Contact job %d (%d number(s)) was still running when this run stopped waiting; its "
                         "answer is read on the next run" % (job["batch"], total))
        else:
            res["not checked"] += total
            ctx.out.line("the texting app was still looking %d number(s) up when this run stopped waiting" % total)

    if live:
        store.kv_set(SC_QUEUE, json.dumps(queue))
        store.kv_set(SC_JOBS, json.dumps(jobs))
        stuck = sum(1 for q in queue.values() if int(q.get("tries") or 0) >= TRIES_SAID)
        if stuck:
            ctx.out.problem("%d owner(s) could not be taken out of Smarter Contact after %d or more tries; they stay "
                            "queued and are tried on every run" % (stuck, TRIES_SAID))
    return res


def _sc_tally(ctx: Ctx, res: Counter) -> None:
    names = {"marked": "Smarter Contact: marked do-not-contact",
             "would_mark": "Smarter Contact: would be marked do-not-contact",
             "already": "Smarter Contact: already on its do-not-text list",
             "not_in_sc": "Smarter Contact: number not there",
             "lead": "Smarter Contact: a new lead's number, left for a person",
             "bad_number": "Smarter Contact: not a ten-digit number",
             "failed": "Smarter Contact: failed, tried again next run" if ctx.live else "Smarter Contact: failed",
             "not checked": "Smarter Contact: not checked this run"}
    for k, n in res.items():
        if n:
            ctx.out.tally[names.get(k, "Smarter Contact: %s" % k)] += n


def run_revoke(ctx: Ctx) -> None:
    st = ctx.stage
    cfg = getattr(ctx.policy, "gate", None) or gate.GateConfig()
    sync = activity.sync(ctx.crm, "state", now=ctx.now, floor_days=int(st.get("floor_days", 3)),
                         max_calls=int(st.get("max_log_calls", 400)))
    # The log copy holds everything only through `through` while it is still catching up. This run
    # reads to there and its mark stops there, or the changes not read yet would fall behind the mark
    # and never be handled. With nothing read through at all, the mark does not move.
    end = ctx.now
    if not sync["complete"]:
        ctx.out.problem("the change log is still catching up (read through %s)" % (sync["through"] or "nothing yet"))
        try:
            end = min(ctx.now, datetime.fromisoformat(sync["through"])) if sync["through"] else None
        except (TypeError, ValueError):
            end = None
    mark = KV if ctx.live else KV_PLAN
    last = store.kv_get(mark)
    since = datetime.fromisoformat(last) if last else ctx.now - timedelta(hours=float(st.get("first_window_hours", 24)))
    why: dict[str, str] = {}
    kinds: dict[str, str] = {}
    keys: dict[str, dict[str, str]] = defaultdict(dict)     # record -> {reason key: why}
    for e in activity.events(("property.status.updated", "property.tags.added"), since=since, until=end):
        reason, kind, key = _suppressing_key(e, cfg)
        if reason and e["resource"]:
            why[e["resource"]] = reason
            kinds[e["resource"]] = "stop" if "stop" in (kind, kinds.get(e["resource"])) else kind
            keys[e["resource"]][key] = reason
    ctx.out.found = len(why)
    by = Counter(why.values())
    for reason, n in by.most_common(6):
        ctx.out.tally["just became untouchable: %s" % reason] = n
    pulled = pull_from_batches(ctx, set(why), why) if why else 0
    allowed = _sc_reasons(ctx)
    if allowed is None:
        sc_why, sc_kinds = why, kinds
    else:
        # Smarter Contact sees only the reasons sc_reasons names, judged on those reasons alone: a
        # record that became a lead and also sold is looked up as a lead when sold is not listed.
        sc_why, sc_kinds = {}, {}
        for u in why:
            mine = {k: w for k, w in keys[u].items() if k in allowed}
            if not mine:
                ctx.out.tally["kept out of Smarter Contact (reason not in sc_reasons)"] += 1
                continue
            stops = [w for k, w in mine.items() if k != "lead"]
            sc_why[u] = stops[-1] if stops else mine["lead"]
            sc_kinds[u] = "stop" if stops else "lead"
    res = pull_from_smarter_contact(ctx, sc_why, sc_kinds)
    _sc_tally(ctx, res)
    if res["lead"]:
        ctx.out.line("%d number(s) of new leads are still in Smarter Contact; take them out of their campaign by hand" % res["lead"])
    if end is not None and end > since:
        store.kv_set(mark, end.isoformat(timespec="seconds"))
    sc_acted = res["marked"] if ctx.live else res["would_mark"]
    ctx.out.acted = pulled + sc_acted
    ctx.out.writes += sc_acted          # each one a write in Smarter Contact (made, or that a live run would make)
    uses_sc = bool(st.get("smarter_contact", True))
    sc_words = ""
    if uses_sc:
        sc_words = (" and marked %d Smarter Contact number(s) do-not-contact" if ctx.live else
                    " and mark %d Smarter Contact number(s) do-not-contact") % sc_acted
    left = len(queued()) if uses_sc and ctx.live else 0
    tail = (" %d owner(s) wait for the next run to be taken out of Smarter Contact." % left) if left else ""
    if not why:
        carried = ""
        if uses_sc and sc_acted:
            carried = " %s %d Smarter Contact number(s) do-not-contact for owners an earlier run could not finish." % (
                "Marked" if ctx.live else "Would mark", sc_acted)
        ctx.out.head = "Nobody became untouchable since the last check.%s%s" % (carried, tail)
        return
    verb = "Pulled" if ctx.live else "Would pull"
    ctx.out.head = "%d record(s) just became untouchable. %s %d staged piece(s)%s.%s" % (len(why), verb, pulled, sc_words, tail)
