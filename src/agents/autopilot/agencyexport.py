"""The agency export: the monthly file for the calling agency (the owner's decision, 2026-11-13).

The agency track is everything in the buy box that is not first to market and not the Top 1,000.
Once a month this stage ranks it by the same Dream 100 score as the Top 1,000 (top1000.py, the
same code), one owner one slot, takes the top 5,000 owners not sent in the last 90 days, and builds
the file in the format the agency already maps: agency_call_list's own exporter, the October file
(Record ID, owner, property and mailing address, Phone 1 to 10, emails, the AI scores, one row per
owner with the owner's other houses listed). It is staged as a batch for a person:

    release batch N        shows the file's link
    confirm N              checks the batch again and rewrites the file without anyone who changed
    confirm N uploaded     after the person has sent the file: every record of every owner in it
                           goes on the list Agency with the date field Sent to Agency set to that day

WHY 90 DAYS IS REMEMBERED HERE. The rule protects a homeowner from being called by the agency twice
in a quarter, so it cannot rest on a list a person can edit: the day each record was sent is the
autopilot's own memory (one fact per record), and the list and the date field are its mirror in
DataSift. A record on the list with no date the autopilot knows has its date field read (a few
hundred a run) before it can be sent again.

WHO IS NOT SENT: an owner in a higher lane on any house (first to market, the Top 1,000, a lead,
an obituary), and anything the suppression gate holds from a caller, checked when the record is
read and again at release. An owner the agency cannot dial (no callable number, an entity, held by
the exporter's own rules) is left out of the file and is not tried again for the same 90 days, so a
month's 5,000 are not spent on the same owners again.

WHAT IT COSTS. The ranking is the month's shared SiftMap sweep and the account's own search rows.
Every house of a candidate owner is opened once (the call list needs every phone, and only the
record holds them; the gate needs each house it hands over), a few thousand reads a run, kept for a
few days, so the reading spreads over the night slots. Marking a sent file is two writes a record,
far more than one release may make inside an hour's share of the account's requests, so a release
marks the first records (the first is read back), remembers every record as sent at once, and the
stage's night runs mark the rest under the same run id (one undo puts the whole file's marks back).
Those queued marks are written only on a live box; they are the release's own work, a person's go,
not a stage decision.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config, store
from . import activity, budget, ledger, top1000
from .stages import Ctx, Summary

KIND = "agency"
TRACK = "agency"
FACT_SENT = "agency_sent"           # the day the record was in a file the agency got ("" = checked, never sent)
FACT_UNSENDABLE = "agency_unsendable"
QUEUE_KEY = "autopilot:agency:marking"
DEFAULTS = {
    "size": 5000, "resend_days": 90, "margin": 0.10, "list": "Agency", "field": "Sent to Agency",
    "list_name": "Agency call list", "about": "", "after_stage": "top1000_refresh", "max_reads": 2500,
    "max_writes": 1200, "release_writes": 100, "release_reads": 400, "max_field_reads": 300, "plan_harvest": True,
    # Decision D32 (2026-11-13): a number DataSift flags from the do-not-call registry is mail only until
    # the attorney answers whether it may be called. The agency calls, so "hold" keeps every number the
    # autopilot has seen flagged out of the file; "send" leaves the file as agency_call_list builds it.
    "registry_numbers": "hold",
}


def options(st) -> dict:
    o = top1000.options(st)
    o["size"] = int(st.get("size") or DEFAULTS["size"])
    for k, v in DEFAULTS.items():
        if k == "size":
            continue
        got = st.get(k)
        o[k] = v if got is None else got
    return o


def block_problems(st) -> list[str]:
    out = top1000.block_problems(st)
    for key, low in (("resend_days", 1), ("max_writes", 0), ("release_writes", 1), ("release_reads", 0),
                     ("max_field_reads", 0)):
        v = st.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
            out.append("stage %s: %s must be a whole number, %d or more" % (st.id, key, low))
    m = st.get("margin")
    if m is not None and (isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 1):
        out.append("stage %s: margin is the share of extra owners read to fill the file, from 0 up to 1" % st.id)
    for key in ("list", "field"):
        if key in (st.params or {}) and not str(st.get(key) or "").strip():
            out.append("stage %s: %s may not be empty" % (st.id, key))
    if st.get("registry_numbers") is not None and st.get("registry_numbers") not in ("hold", "send"):
        out.append("stage %s: registry_numbers is hold (decision D32: mail only) or send" % st.id)
    return out


def registry_hold(o: dict, slims: list[dict]) -> int:
    """Take every number the autopilot has seen flagged from the do-not-call registry (the scrub's
    memory, which every stage's search rows feed) out of these call-list records, in place. Returns
    how many numbers came out. A search row shows the flag for one number only, so a number never
    seen on a row is not known either way and stays."""
    if str(o.get("registry_numbers") or "hold") != "hold":
        return 0
    from . import gate, scrub
    flagged = scrub.flagged_among([p.get("number") for s in slims for p in s.get("phones") or []])
    if not flagged:
        return 0
    out = 0
    for s in slims:
        keep = [p for p in s.get("phones") or [] if gate.digits(p.get("number")) not in flagged]
        out += len(s.get("phones") or []) - len(keep)
        s["phones"] = keep
    return out


# ---------------------------------------------------------------------- what was sent, remembered
def _day(ctx) -> str:
    return ledger.day(ctx.now, ctx.policy.tz)


def sent_owners(ctx, o: dict, pool: dict) -> tuple[set, set, Counter]:
    """(owners sent inside resend_days, owners held because their send date could not be read this
    run, counts). The day comes from the autopilot's own facts; a record on the Agency list (the pool
    reads the list) with no remembered day has its Sent to Agency field read, at most
    max_field_reads a run, and the answer is remembered (an empty field: never sent by this process)."""
    stats: Counter = Counter()
    today = ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date()
    horizon = today - timedelta(days=int(o["resend_days"]))
    owner_of = {u: p.get("owner") or u for u, p in pool.items()}
    on_list = "list %s" % o["list"]
    members = {u for u, p in pool.items() if on_list in (p.get("from") or [])}
    known = top1000.facts_of(FACT_SENT)
    sent, unknown = set(), set()
    field_reads = 0
    for u in owner_of:
        v = known[u][0] if u in known else None
        if v is None and u in members:
            if field_reads >= int(o["max_field_reads"]):
                unknown.add(owner_of.get(u, u))
                stats["on the Agency list with no send date read yet (held this run)"] += 1
                continue
            field_reads += 1
            try:
                v = str(ctx.crm.custom_fields(u).get(str(o["field"])) or "")[:10]
            except budget.BudgetExhausted:
                raise
            except Exception:  # noqa: BLE001  (a field that cannot be read holds the record this run, and is said)
                unknown.add(owner_of.get(u, u))
                stats["send date could not be read (held this run)"] += 1
                continue
            ledger.set_fact(u, FACT_SENT, v)       # knowledge: what the record's own field says
        if not v:
            continue
        try:
            if datetime.fromisoformat(str(v)[:10]).date() >= horizon:
                sent.add(owner_of.get(u, u))
        except ValueError:
            continue
    stats["send dates read from the field"] = field_reads
    return sent, unknown, stats


def unsendable(owner_houses: list[str], days: int, today, known: dict) -> str:
    """Why this owner could not be handed to the agency inside the last `days`, or ''. `known` is
    facts_of(FACT_UNSENDABLE), read once for the whole pool. The day compared is the business day
    the fact names, the run's own clock, never when the row was written."""
    for u in owner_houses:
        got = known.get(u)
        if not got:
            continue
        f = got[0]
        if not isinstance(f, dict) or not f.get("why"):
            continue
        try:
            if (today - datetime.fromisoformat(str(f.get("day"))[:10]).date()).days <= days:
                return f["why"]
        except (TypeError, ValueError):
            continue
    return ""


def top1000_owners(ctx, o: dict, mode: str, month: str) -> tuple[bool, set, str]:
    """(wait, the owners this month's Top 1,000 holds, a line to say). The agency file waits for the
    month's Top 1,000 so an owner promoted this month is not sent away the same night. A plan waits
    for the plan; a live file waits for the live refresh, and when that stage is not live only the
    records holding the tag today are kept out."""
    sid = str(o.get("after_stage") or "")
    st = (getattr(ctx.policy, "stages", None) or {}).get(sid)
    if not sid or st is None or st.mode == "off":
        return False, set(), ""
    if mode == "live" and st.mode != "live":
        return False, set(), "The Top 1,000 stage is not live, so only the records holding its tag today were kept out."
    s = top1000.load_json(top1000.state_path(sid, month, mode), {})
    if s.get("phase") != "done":
        return True, set(), ""
    return False, set(s.get("owners") or []), ""


# ---------------------------------------------------------------------- the stage
def run_agencyexport(ctx: Ctx) -> None:
    st = ctx.stage
    o = options(st)
    drain(ctx, o)
    month = top1000.month_of(ctx)
    mode = "live" if ctx.live else "plan"
    spath = top1000.state_path(st.id, month, mode)
    state = top1000.load_json(spath, {}) or {"month": month, "mode": mode, "started": ctx.now.isoformat(timespec="seconds")}
    if state.get("phase") == "done":
        ctx.out.head = ctx.out.head or "Nothing to do: this month's agency file was built %s. %s" % (
            str(state.get("done") or "")[:10], (state.get("summary") or {}).get("line") or "")
        return
    wait, promoted, note = top1000_owners(ctx, o, mode, month)
    if wait:
        ctx.out.head = ctx.out.head or "Waiting for this month's Top 1,000 to finish before the agency file is built."
        return
    if note:
        ctx.out.line(note)
    sift = top1000.month_sweep(ctx, o)
    if sift is None:
        return
    pool = top1000.cached_pool(ctx, o, spath.with_name(spath.stem + "-pool.json"), list(st.presets), (),
                               [str(o["list"])])
    if pool is None:
        return
    for kind in ("tag", "list"):
        if top1000.index(ctx.crm, kind) is None:
            ctx.out.problem("the account's %s index could not be read, so the houses' %ss are unknown; nothing was ranked" % (kind, kind))
            return
    sent, unknown, sent_stats = sent_owners(ctx, o, pool)
    for k, n in sent_stats.items():
        if n:
            ctx.out.tally[k] = n
    build = top1000.scoring(o)
    try:
        _work(ctx, o, build, sift, pool, sent, unknown, promoted, state, spath)
    finally:
        top1000.restore(build)


def _work(ctx, o, build, sift, pool, sent, unknown, promoted, state, spath) -> None:
    st = ctx.stage
    days = int(o["resend_days"])
    by_owner: dict = {}
    for u, p in pool.items():
        by_owner.setdefault(p.get("owner") or u, []).append(u)
    known = top1000.facts_of(FACT_UNSENDABLE)

    def skip(u, key):
        if key in promoted:
            return "in this month's Top 1,000"
        if key in sent:
            return "sent to the agency in the last %d days" % days
        if key in unknown:
            return "send date not known yet (held this run)"
        why = unsendable(by_owner.get(key, [u]), days, ctx.now.astimezone(ZoneInfo(ctx.policy.tz)).date(), known)
        if why:
            return "not sendable last time (%s), not tried again for %d days" % (why, days)
        return ""

    owners, out_by = top1000.candidates(ctx, build, sift, pool, TRACK, top1000.d4d_scores(o), skip=skip,
                                        held_lists=o["exclude_lists"])
    order = top1000.ranked(owners)
    pick = order[: int(math.ceil(o["size"] * (1 + float(o["margin"]))))]
    ctx.out.found = len(owners)
    harvest = bool(ctx.live or o["plan_harvest"])
    if not harvest:
        n = sum(len(owners[k]["houses"]) for k in pick)
        ctx.out.head = ("Would read %d record(s) of the top %d owners to build the agency file (plan_harvest is off, so "
                        "this plan reads none)." % (n, len(pick)))
        return
    rd = top1000.Reader(ctx, o["max_reads"], o["facts_days"])
    left = 0
    for key in pick:
        for h in owners[key]["houses"]:
            rec = rd.get(h["uuid"])
            if rec is None:
                left += 1
            else:
                h["rec"] = rec
    if left:
        top1000.reread_note(ctx, rd, left)
        top1000.save_json(spath, dict(state, phase="read", updated=ctx.now.isoformat(timespec="seconds")))
        ctx.out.head = "Reading the top owners for this month's agency file: %d record(s) still to read." % left
        return
    import agency_call_list as acl
    kb = acl.kb
    final: dict = {}
    gone: Counter = Counter()
    registry_out = 0
    for key in pick:
        houses, main = [], None
        dropped = ""
        for h in owners[key]["houses"]:
            rec = h.get("rec") or {}
            if rec.get("_gone"):
                gone["the record could not be read"] += 1
                continue
            v = ctx.verdict(rec, "assign", lane=TRACK)
            held = top1000.on_held_list(rec.get("lists"), o["exclude_lists"])
            if held:
                dropped = "on the %s list" % held
                break
            if not v.ok:
                if v.code in top1000.OWNER_LEVEL:
                    dropped = v.reason()
                    break
                gone[v.reason()] += 1
                continue
            houses.append(h)
        if dropped:
            gone["owner left out whole: %s" % dropped] += len(owners[key]["houses"])
            _remember_unsendable(ctx, owners[key]["houses"], dropped)
            continue
        if not houses:
            continue
        sc = top1000.owner_score(build, houses, top1000.reach_of(build, (houses[0].get("rec") or {}))[0])
        main = next(h for h in houses if h["uuid"] == sc["best"])
        slim = kb._slim(main["rec"])
        why = acl.held_reason(slim)
        took = registry_hold(o, [slim])
        registry_out += took
        phones, _ = kb.clean_phones(slim)
        if not why and not phones:
            why = "every number is on the do-not-call registry (mail only)" if took else "no callable number"
        if why:
            gone["not sendable: %s" % why.split(":")[0].split(" (")[0]] += 1
            _remember_unsendable(ctx, houses, why.split(":")[0])
            continue
        final[key] = {"houses": houses, "score": sc, "main": main}
    chosen = sorted(final, key=lambda k: (-final[k]["score"]["score"], -final[k]["score"]["room"], k))[: o["size"]]
    for why, n in list(out_by.most_common()) + list(gone.most_common()):
        ctx.out.tally["left out: %s" % why] += n
    if not chosen:
        ctx.out.head = "No owner in the agency track is due to be sent this month."
        top1000.save_json(spath, dict(state, phase="done", done=ctx.now.isoformat(timespec="seconds"),
                                      summary={"line": "Agency file for %s: nobody was due." % state["month"]}))
        return
    mdir = top1000.tracks_dir() / ("%s-%s-%s" % (st.id, state["month"], "live" if ctx.live else "plan"))
    fdir = top1000.files_dir(ctx)
    entries, records = [], {}
    for rank, key in enumerate(chosen, 1):
        f = final[key]
        others = [h for h in f["houses"] if h["uuid"] != f["main"]["uuid"]]
        for h in [f["main"]] + others:
            s = sift.get(h["uuid"]) or {}
            entries.append({"dataflik_id": h["uuid"], "rank": rank, "realtor_score": s.get("r"),
                            "investor_off_market_score": s.get("i"), "rung": segment_of(pool.get(h["uuid"]) or {})})
            records[h["uuid"]] = kb._slim(h["rec"])
    registry_hold(o, list(records.values()))       # the file carries no number held by decision D32
    if registry_out:
        ctx.out.tally["numbers held out of the file: on the do-not-call registry (D32, mail only)"] = registry_out
    files = export(o, mdir, fdir, entries, records)
    if not files.get("csv"):
        ctx.out.problem("the agency file could not be written (%s); nothing was staged" % (files.get("error") or "no file"))
        return
    where = top1000.link(ctx, Path(files["csv"]))
    houses_n = sum(len(final[k]["houses"]) for k in chosen)
    rows = files.get("summary", {}).get("rows", len(chosen))
    ctx.out.line("The file: %s (%s rows; the workbook beside it has the Read me and the held-out sheet)." % (where, rows))
    for i, k in enumerate(chosen[:3], 1):
        f = final[k]
        ctx.out.line("rank %d: %s, score %.1f, %d house(s)" % (i, f["main"].get("street") or f["main"]["uuid"][:8],
                                                             f["score"]["score"], len(f["houses"])))
    if len(chosen) < o["size"]:
        ctx.out.line("%d owners were sendable, short of %s (raise margin to read further down the ranking)." % (
            len(chosen), format(o["size"], ",")))
    items = [{"record_uuid": final[k]["main"]["uuid"], "owner_uuid": k, "records": [h["uuid"] for h in final[k]["houses"]],
              "street": final[k]["main"].get("street") or "", "rank": i} for i, k in enumerate(chosen, 1)]
    info = {"stage": st.id, "month": state["month"], "mode": "live" if ctx.live else "plan", "dir": str(mdir),
            "files": str(fdir), "csv": Path(files["csv"]).name, "task": int(ctx.task_id or 0), "link": where,
            "staged": ctx.now.isoformat(timespec="seconds")}
    summary = {"owners": len(chosen), "houses": houses_n, "file": files["csv"], "link": where}
    if not ctx.live:
        ctx.out.acted = len(chosen)
        ctx.out.head = ("Would send %d owners (%d houses) to the calling agency. Nothing is sent or marked in plan mode."
                        % (len(chosen), houses_n))
        top1000.save_json(spath, dict(state, phase="done", done=ctx.now.isoformat(timespec="seconds"), info=info,
                                      summary=dict(summary, line="Agency file for %s (plan): %d owners on %d houses." % (
                                          state["month"], len(chosen), houses_n))))
        return
    expired = expire_older(ctx)
    bid = ledger.batch_create(st.id, KIND, items, 0.0)
    store.kv_set(_info_key(bid), json.dumps(info))
    ctx.out.batch = bid
    ctx.out.acted = len(chosen)
    if expired:
        ctx.out.problem("the last agency file (batch %s) was never sent, so it was closed; its owners were picked up again"
                        % ", ".join(str(b) for b in expired))
    ctx.out.head = ("Staged batch %d: %d owners (%d houses) for the calling agency. Nothing is sent or marked until a "
                    "person releases it: release batch %d shows the file." % (bid, len(chosen), houses_n, bid))
    top1000.save_json(spath, dict(state, phase="done", done=ctx.now.isoformat(timespec="seconds"), info=info, batch=bid,
                                  summary=dict(summary, line="Agency file for %s: %d owners on %d houses, staged as batch %d." % (
                                      state["month"], len(chosen), houses_n, bid))))


def segment_of(p: dict) -> str:
    """The Segment column: the part of the pool preset's title in parentheses (Strong, Bulk), else
    the title itself, else Agency."""
    src = str(p.get("src") or "")
    if "(" in src and src.endswith(")"):
        return src[src.rindex("(") + 1:-1].strip()
    return src.replace("preset ", "", 1).strip() or "Agency"


def _remember_unsendable(ctx, houses: list[dict], why: str) -> None:
    """An owner the agency could not be handed is not read again for resend_days: knowledge about
    the record, kept in plan mode too, so a month's reads go to owners who can be sent."""
    stamp = {"day": _day(ctx), "why": str(why)[:80]}
    for h in houses:
        ledger.set_fact(h["uuid"], FACT_UNSENDABLE, stamp)


def export(o: dict, mdir: Path, fdir: Path, entries: list[dict], records: dict) -> dict:
    """Build the call list with agency_call_list's own exporter, so the agency gets exactly the
    October format it already maps. The exporter reads a pool file and a record cache from a run
    folder and names its files itself; its two workstation lookups (the pull_by_ids state and the
    repo's output folder) are pointed at this month's folder for the call and put back after."""
    import agency_call_list as acl
    mdir.mkdir(parents=True, exist_ok=True)
    top1000.save_json(mdir / "pool.json", entries)
    top1000.save_json(mdir / "records.json", records)
    ids = {e["dataflik_id"] for e in entries}
    args = argparse.Namespace(pool=str(mdir / "pool.json"), list_name=str(o["list_name"]),
                              about=str(o.get("about") or "The agency track: owners not first to market and not in the Top 1,000."),
                              not_on=[])
    saved = (acl.uuid_map, acl.OUT)
    acl.uuid_map = lambda _a: {u: u for u in ids}
    acl.OUT = Path(fdir)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            acl.phase_export(args, mdir)
    except Exception as exc:  # noqa: BLE001  (the stage says it and stages nothing)
        return {"error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
    finally:
        acl.uuid_map, acl.OUT = saved
    # The exporter dates its own file names; the newest call list in the folder is the one just written.
    made = sorted((p for p in Path(fdir).glob("%s_Call_List_*.csv" % acl.slug(str(o["list_name"])))
                   if not p.name.endswith("_no_phones.csv")), key=lambda p: p.stat().st_mtime)
    if not made:
        return {"error": "the exporter wrote no call list"}
    csv_path = made[-1]
    return {"csv": str(csv_path), "xlsx": str(csv_path.with_suffix(".xlsx")),
            "summary": top1000.load_json(mdir / "export_summary.json", {})}


def expire_older(ctx) -> list[int]:
    """A month's file is that month's: an earlier one nobody sent is closed when the next is staged,
    so two files never hold the same owner."""
    out = []
    for b in ledger.batches(status="staged", stage=ctx.stage.id):
        if b["kind"] == KIND:
            ledger.batch_set(b["id"], "expired", {"why": "a newer agency file was staged"})
            out.append(b["id"])
    return out


# ---------------------------------------------------------------------- release (agents/release.py routes here)
def _info_key(batch_id: int) -> str:
    return "autopilot:agency:batch:%d" % int(batch_id)


def info_of(batch_id: int) -> dict:
    try:
        return json.loads(store.kv_get(_info_key(batch_id)) or "{}")
    except ValueError:
        return {}


def file_link(info: dict) -> str:
    if info.get("task") and info.get("csv"):
        from .. import slack
        url = slack.file_url(int(info["task"]), info["csv"])
        if url:
            return url
    return str(Path(info.get("files") or "") / (info.get("csv") or ""))


def release_note(b: dict) -> str:
    """What `release batch N` adds for an agency file: where it is and the two confirms."""
    info = info_of(b["id"])
    return ("The file: %s . Send it to the calling agency, then reply `confirm %d uploaded` and every record in it is "
            "marked sent. `confirm %d` first checks the batch again and rewrites the file without anyone who changed."
            % (file_link(info) or "not found", b["id"], b["id"]))


def recheck(ctx, b: dict, cap: int, staged: str = "") -> tuple[list[dict], Counter, list[str], list[str]]:
    """(the items that still pass, the held ones counted by reason, problems, the held owners' streets
    with their reason). Every owner whose records
    changed in the account's log since the batch was staged (a status, a tag, a list) is read again
    and checked by the gate; everything else stands as it was checked at staging. Reading all five
    thousand again would be past an hour's share of the account's requests. `staged` is the moment
    the stage staged the file, on the run's own clock."""
    problems = []
    sync = activity.sync(ctx.crm, "state", now=ctx.now, max_calls=200)
    if not sync["complete"]:
        problems.append("the account's log copy is behind (read through %s), so a change after it was not seen" % (
            sync["through"] or "nothing yet"))
    try:
        since = datetime.fromisoformat(staged or b.get("created") or "")
    except ValueError:
        since = ctx.now - timedelta(days=30)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    changed = {e["resource"] for e in activity.events(activity.STATE, since=since, until=ctx.now + timedelta(seconds=1))}
    rd = top1000.Reader(ctx, cap, 0)
    keep, held, named = [], Counter(), []
    for it in b["items"]:
        mine = [u for u in it.get("records") or [it["record_uuid"]] if u in changed]
        why = ""
        for u in mine:
            rec = rd.get(u)
            if rec is None:
                problems.append("the release's read cap (%d) was reached; the owners after it were not checked again" % cap)
                break
            if rec.get("_gone"):
                continue
            v = ctx.verdict(rec, "assign", lane=TRACK)
            if not v.ok:
                why = v.reason()
                break
        if why:
            held[why] += 1
            named.append("%s (%s)" % (it.get("street") or it["record_uuid"][:8], why))
        else:
            keep.append(it)
    return keep, held, list(dict.fromkeys(problems)), named


def release(batch_id: int, *, crm=None, pol=None, commit: bool = False, uploaded: bool = False,
            now: datetime | None = None) -> dict:
    """Release an agency file batch. Without commit it reports. With commit and no `uploaded` it checks
    the batch again and rewrites the file without anyone who changed (the check that matters, right
    before the file leaves the building); the batch stays staged. With `uploaded` the file has gone:
    every record of every owner in it is remembered as sent today, marked in DataSift (the list and
    the date field, the first record read back), and anyone who no longer passes is said out loud,
    because the agency already has them."""
    from . import policy as policy_mod
    b = ledger.batch(int(batch_id))
    if not b:
        raise SystemExit("no batch %d" % batch_id)
    if b["kind"] != KIND:
        raise SystemExit("batch %d is a %s batch" % (batch_id, b["kind"]))
    if b["status"] != "staged":
        raise SystemExit("batch %d is %s, not staged" % (batch_id, b["status"]))
    pol = pol or policy_mod.load()
    st = pol.stage(b["stage"])
    o = options(st)
    if crm is None:
        from .crm import CRM
        crm = CRM()
    now = now or datetime.now(timezone.utc)
    out = Summary(stage=st.id, kind=st.kind, owner=st.owner, mode=st.mode, live=commit)
    ctx = Ctx(policy=pol, stage=st, crm=crm, run_id="agency-%d" % int(batch_id), now=now, live=commit, out=out)
    info = info_of(int(batch_id))
    keep, held, problems, named = recheck(ctx, b, int(o["release_reads"]), info.get("checked") or info.get("staged") or "")
    records = sum(len(it.get("records") or []) for it in b["items"])
    if not commit:
        return {"batch": int(batch_id), "kind": KIND, "owners": len(b["items"]), "records": records,
                "would_hold": dict(held), "file": file_link(info), "problems": problems, "dry": True}
    if not uploaded:
        if held:
            files = _rewrite(o, info, keep)
            if files.get("csv"):
                info["csv"] = Path(files["csv"]).name
            else:
                problems.append("the file could not be rewritten (%s); the earlier file still holds the held owners"
                                % (files.get("error") or "no file"))
            ledger.batch_items_set(int(batch_id), keep, 0.0)
        # The upload's own check then reads only what changed after this one.
        info["checked"] = now.isoformat(timespec="seconds")
        store.kv_set(_info_key(int(batch_id)), json.dumps(info))
        return {"batch": int(batch_id), "kind": KIND, "owners": len(keep), "held": dict(held), "file": file_link(info),
                "results": {"owners in the file": len(keep), "held back since it was staged": sum(held.values())},
                "problems": problems, "next": "send the file to the calling agency, then confirm %d uploaded" % int(batch_id)}
    if named:
        problems.append("%d owner(s) in the sent file no longer pass; tell the agency to skip them: %s%s" % (
            len(named), "; ".join(named[:10]), "; and %d more" % (len(named) - 10) if len(named) > 10 else ""))
    return _mark_sent(ctx, o, b, problems)


def _rewrite(o: dict, info: dict, keep: list[dict]) -> dict:
    mdir = Path(info.get("dir") or "")
    entries = top1000.load_json(mdir / "pool.json", [])
    records = top1000.load_json(mdir / "records.json", {})
    ids = {u for it in keep for u in it.get("records") or [it["record_uuid"]]}
    return export(o, mdir, Path(info.get("files") or mdir / "files"), [e for e in entries if e.get("dataflik_id") in ids],
                  {u: r for u, r in records.items() if u in ids})


def add_list(crm, uuid: str, title: str) -> None:
    """The documented per-record route; `lists` is one title as a string (an array is accepted and does nothing)."""
    crm.call("/api/internal/property/%s/add-lists/" % uuid, "POST", {"lists": title})


def _mark(ctx, u: str, title: str, field: str, day: str, before: str) -> None:
    ctx.change(u, "add_lists", [], [title])
    ctx.write("list", add_list, ctx.crm, u, title)
    ctx.change(u, "custom_field", {"field_uuid": field, "value": before or ""}, {"field_uuid": field, "value": day})
    ctx.write("field", ctx.crm.set_custom_field, u, field, day)


def _mark_sent(ctx, o: dict, b: dict, problems: list[str]) -> dict:
    title, label = str(o["list"]), str(o["field"])
    batch_id = int(b["id"])
    if not ctx.crm.list_uuid(title):
        problems.append("the list %s does not exist in this account; create it, then confirm %d uploaded again" % (title, batch_id))
        return {"batch": batch_id, "kind": KIND, "sent": 0, "of": len(b["items"]), "problems": problems}
    field = ctx.crm.custom_field_uuid(label)
    if not field:
        problems.append("the date field %s does not exist in this account; create it, then confirm %d uploaded again" % (label, batch_id))
        return {"batch": batch_id, "kind": KIND, "sent": 0, "of": len(b["items"]), "problems": problems}
    day = _day(ctx)
    uuids = [u for it in b["items"] for u in dict.fromkeys(it.get("records") or [it["record_uuid"]])]
    before = {u: str(ledger.fact(u, FACT_SENT) or "") for u in uuids}
    for u in uuids:
        ledger.set_fact(u, FACT_SENT, day)        # the 90-day rule reads this, whatever happens to the marks below
        ledger.event(ctx.run_id, ctx.stage.id, u, "agency_sent", {"batch": batch_id}, now=ctx.now, tz=ctx.policy.tz)
    marked = 0
    first = uuids[0] if uuids else ""
    if first:
        try:
            _mark(ctx, first, title, field, day, before[first])
            rec = ctx.crm.record(first)
            on_list = title.lower() in {t.strip().lower() for t in top1000._titles(rec.get("lists"))}
            shown = str(ctx.crm.custom_fields(first).get(label) or "")[:10]
            if not on_list or shown != day:
                problems.append("the marks did not stick on %s (on the list: %s, date field: %s); nothing more was marked"
                                % (_street_of(b, first), "yes" if on_list else "no", shown or "empty"))
                ledger.batch_set(batch_id, "sent", {"owners": len(b["items"]), "records": len(uuids), "marked": 0,
                                                     "problems": problems})
                return {"batch": batch_id, "kind": KIND, "sent": len(b["items"]), "of": len(b["items"]), "problems": problems}
            marked = 1
        except budget.BudgetExhausted as exc:
            problems.append("stopped marking early: %s" % exc)
    rest = uuids[marked:]
    now_part, later = rest[: max(0, int(o["release_writes"]) - marked)], rest[max(0, int(o["release_writes"]) - marked):]
    for i, u in enumerate(now_part):
        try:
            _mark(ctx, u, title, field, day, before[u])
            marked += 1
        except budget.BudgetExhausted as exc:
            problems.append("stopped marking early: %s" % exc)
            later = now_part[i:] + later
            break
        except Exception as exc:  # noqa: BLE001  (one record failing must not stop the rest)
            problems.append("could not mark %s (%s)" % (_street_of(b, u), str(exc)[:80]))
    if later:
        queue = _queue()
        queue.append({"batch": batch_id, "day": day, "list": title, "field": field, "records": later,
                      "before": {u: before[u] for u in later}})
        store.kv_set(QUEUE_KEY, json.dumps(queue))
    ledger.batch_set(batch_id, "sent", {"owners": len(b["items"]), "records": len(uuids), "marked": marked,
                                         "queued": len(later), "problems": problems})
    return {"batch": batch_id, "kind": KIND, "sent": len(b["items"]), "of": len(b["items"]),
            "results": {"records marked now": marked, "left for the stage's night runs": len(later)}, "problems": problems}


def _street_of(b: dict, u: str) -> str:
    for it in b.get("items") or []:
        if u == it.get("record_uuid") or u in (it.get("records") or []):
            return it.get("street") or u[:8]
    return u[:8]


def _queue() -> list:
    try:
        q = json.loads(store.kv_get(QUEUE_KEY) or "[]")
    except ValueError:
        q = []
    return q if isinstance(q, list) else []


def drain(ctx, o: dict) -> None:
    """Mark the records a released file left for later, at most max_writes a run, under the release's
    own run id so one undo puts a whole file's marks back. Only on a live box: these writes are the
    release's, approved by the person who confirmed it, so they do not wait on the stage's mode."""
    queue = _queue()
    if not queue:
        return
    waiting = sum(len(q.get("records") or []) for q in queue)
    if not config.live():
        ctx.out.line("%d record(s) of a sent agency file wait to be marked; they are marked once the box is live." % waiting)
        return
    cap, marked, rest = int(o["max_writes"]), 0, []
    for q in queue:
        sub = Ctx(policy=ctx.policy, stage=ctx.stage, crm=ctx.crm, run_id="agency-%d" % int(q["batch"]), now=ctx.now,
                  live=True, out=ctx.out)
        todo = list(q.get("records") or [])
        while todo and marked < cap:
            u = todo[0]
            try:
                _mark(sub, u, q["list"], q["field"], q["day"], (q.get("before") or {}).get(u, ""))
            except budget.BudgetExhausted as exc:
                ctx.out.problem("stopped marking early: %s" % exc)
                cap = marked
                break
            except Exception as exc:  # noqa: BLE001  (a record that cannot be marked is said and skipped)
                ctx.out.problem("could not mark a record of agency batch %s (%s)" % (q["batch"], str(exc)[:80]))
                todo.pop(0)
                continue
            todo.pop(0)
            marked += 1
        if todo:
            rest.append(dict(q, records=todo, before={u: (q.get("before") or {}).get(u, "") for u in todo}))
    store.kv_set(QUEUE_KEY, json.dumps(rest))
    left = sum(len(q["records"]) for q in rest)
    ctx.out.line("Marked %d record(s) of a sent agency file (on the list and dated); %d left for the next run." % (marked, left))
    ctx.out.head = "Marked %d record(s) of a sent agency file; %d left." % (marked, left)


# ---------------------------------------------------------------------- files sent before this stage existed
def remember(paths: list[str], day: str) -> dict:
    """Seed the 90-day memory from agency files sent before this stage existed (agency_call_list's
    format: the Record ID column). Knowledge only, on this machine's database: nothing is written to
    DataSift, and a later day already remembered for a record is kept."""
    when = datetime.fromisoformat(str(day)[:10]).date().isoformat()     # a day that does not parse stops here
    known = top1000.facts_of(FACT_SENT)
    seen, kept_later, rows, blank = set(), 0, 0, 0
    for p in paths:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                rows += 1
                u = (row.get("Record ID") or "").strip()
                if not u:
                    blank += 1
                    continue
                have = str((known.get(u) or ("",))[0] or "")[:10]
                if have and have >= when:
                    kept_later += 1
                    continue
                if u not in seen:
                    ledger.set_fact(u, FACT_SENT, when)
                    seen.add(u)
    return {"rows": rows, "remembered": len(seen), "kept_a_later_day": kept_later, "rows_without_a_record_id": blank,
            "day": when}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The agency export's memory of what was sent (the 90-day rule).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("remember", help="remember an agency file sent before this stage existed (no DataSift write)")
    r.add_argument("--csv", action="append", required=True, help="an agency call list CSV (Record ID column); repeatable")
    r.add_argument("--day", required=True, help="the day the file went to the agency, YYYY-MM-DD")
    args = ap.parse_args(argv)
    store.init()
    out = remember(args.csv, args.day)
    print("remembered %(remembered)d record(s) as sent to the agency on %(day)s (%(rows)d rows read; %(kept_a_later_day)d "
          "already had a later day; %(rows_without_a_record_id)d rows had no Record ID). Nothing was written to DataSift." % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
