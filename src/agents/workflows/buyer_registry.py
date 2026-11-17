"""buyer_registry: the deed-verified local buyer registry, refreshed by a bounded job at night.

dispo_buyers.py builds the registry in phases, by hand, on the workstation. This workflow runs the
same phases on the brain, one bounded slice a night, until a refresh is complete, then waits for
the next one:

  step        what it does                                                     cost
  sweep       the full history sweep (rows.json), only when missing or old     free (map search)
  recent      who bought in the last 12 months (the qualification layer)       free (map search)
  hydrate     the deed read for each property not read before, capped a night  free (map detail)
  aggregate   buyers deduped by name and mailing address                       free (local files)
  unmask      the free reverse-address principal, repeat buyers, capped        free (search + detail)
  principals  Enformion BusinessV2 for repeat buyers the free check missed     PAID, live only, capped
  profiles    what each repeat buyer buys                                      free (local files)
  crm_phones  numbers DataSift already holds on the buyers' records            free (DataSift reads)
  trace       SmartSkip for repeat, active buyers with no number yet           PAID, live only, capped
  score       Trestle (with the litigator check) for every unscored number     PAID, live only, capped

BOUNDED. A night does at most AGENTS_BUYER_REGISTRY_MINUTES of work, at most so many deed reads,
reverse addresses and record reads, and stops at the first free step that has more left than its
cap; the next night resumes there. A paid step that runs out of money is left for the next night
too, without holding up the free steps behind it. RESUMABLE because the phases already keep all
they learn on disk (details.json, principals.json, phones.json) and this job keeps its own place
in job.json beside them. A refresh starts when the last one finished more than
AGENTS_BUYER_REGISTRY_EVERY_DAYS ago, or when a person asks in Slack.

WHERE. config.DATA_DIR/dispo_buyers (the brain's volume, or AGENTS_BUYER_REGISTRY_DIR), never the
image's output/ folder, which a deploy wipes. A box with no deed cache would have to read thousands
of deeds through SiftMap's detail route without adding the records, which DataSift's Acceptable use
calls scraping, so the job refuses a cold start past AGENTS_BUYER_REGISTRY_COLD reads and says to
seed the cache from the workstation (output/dispo_buyers/) instead.

KEY ONLY. The searches go through dispo_buyers.KeyOnlySearch (no password fallback), the reverse
addresses through buyer_sweep.MapClient (the address search, never autocomplete), the CRM reads
through the autopilot's budgeted client on the Open API key. Nothing is written to DataSift: the
registry's files are its output, and the dispo blast reads them.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from .. import config
from ..autopilot import ledger
from . import skip_trace as common

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

WORKFLOW = "buyer_registry"
STEPS = ("sweep", "recent", "hydrate", "aggregate", "unmask", "principals", "profiles", "crm_phones", "trace", "score")
PAID = {"principals": 0.10, "trace": 0.15, "score": 0.015}
MIN_BUYS = 2            # the purchase floor Ty set for every paid step on the registry (2026-08-28)


def S(name: str, default: float) -> float:
    return common.setting("AGENTS_BUYER_REGISTRY_" + name, default)


def registry_dir() -> Path:
    import os
    return Path(os.environ.get("AGENTS_BUYER_REGISTRY_DIR") or (Path(config.DATA_DIR) / "dispo_buyers"))


class _Reader:
    """dispo_buyers' harvest calls crm.get_record(uuid); this answers it through the autopilot's
    budgeted client on the Open API key, so the brain never loads the Deal Room client."""

    def __init__(self, crm):
        self._crm = crm

    def get_record(self, uuid_: str):
        return self._crm.record(uuid_)


def _args(**kw) -> SimpleNamespace:
    base = dict(min_interval=S("MAP_GAP_S", 0.75), workers=1, max_pages=200, refresh=False, limit=0,
                recent_days=365, profile_min_buys=MIN_BUYS, require_active=True, retrace=False,
                trace_min_buys=MIN_BUYS, min_buys=MIN_BUYS, active_only=True, commit=False, source="registry",
                include_dnc=False, preset="", unmask_floor=MIN_BUYS)
    base.update(kw)
    return SimpleNamespace(**base)


def _call(fn, args, log: list) -> str:
    """Run one phase with its print output kept for the log file. '' when it finished; the reason
    when it stopped itself (a gate or a missing earlier phase, which dispo_buyers raises as
    SystemExit)."""
    buf = io.StringIO()
    why = ""
    try:
        with contextlib.redirect_stdout(buf):
            fn(args)
    except SystemExit as exc:
        why = str(exc) or "stopped"
    log.extend(buf.getvalue().strip().splitlines()[-12:])
    return why


def _clear(d, *keys) -> None:
    """Forget a phase's last result before calling it. A phase that returns early (the business
    search not configured, nothing to do) writes nothing, and reading the entry afterwards would
    then report, and charge for, the PREVIOUS run's work."""
    s = d.state()
    if any(k in s for k in keys):
        for k in keys:
            s.pop(k, None)
        d._save(d.STATE_PATH, s)


def _load_job(d) -> dict:
    p = Path(d.OUT) / "job.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save_job(d, job: dict) -> None:
    p = Path(d.OUT) / "job.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------------- the steps
def step_sweep(d, ctx) -> tuple[str, str]:
    rows = Path(d.OUT) / "rows.json"
    age_ok = rows.exists() and (time.time() - rows.stat().st_mtime) < S("SWEEP_DAYS", 90) * 86400
    if age_ok and "sweep" in d.state():
        return "done", "the full sweep is %d day(s) old; kept" % int((time.time() - rows.stat().st_mtime) // 86400)
    why = _call(d.phase_sweep, _args(refresh=True, search_client=ctx.search_client), ctx.log)
    return ("stop", why) if why else ("done", "swept again")


def step_recent(d, ctx) -> tuple[str, str]:
    _clear(d, "recent")
    why = _call(d.phase_recent, _args(search_client=ctx.search_client), ctx.log)
    return ("stop", why) if why else ("done", "%s buyer(s) active in the last year" % (d.state().get("recent") or {}).get("buyers", "?"))


def step_hydrate(d, ctx) -> tuple[str, str]:
    todo = len(d.hydrate_todo())
    cap = int(S("DETAILS", 100))
    if todo > S("COLD", 1000) and not common.setting("AGENTS_BUYER_REGISTRY_COLD_OK", 0):
        return "stop", ("%d deeds have never been read on this box; reading them through SiftMap one night at a time is "
                        "the bulk detail pull the Acceptable use calls scraping. Seed the cache from the workstation "
                        "(output/dispo_buyers/details.json and the rest) to %s" % (todo, d.OUT))
    if not todo:
        return "done", "every deed already read"
    why = _call(d.phase_hydrate, _args(limit=cap, detail_client=ctx.detail_client), ctx.log)
    if why:
        return "stop", why
    left = len(d.hydrate_todo())
    return ("more", "read %d deed(s), %d left" % (min(cap, todo), left)) if left else ("done", "read %d deed(s)" % todo)


def step_aggregate(d, ctx) -> tuple[str, str]:
    why = _call(d.phase_aggregate, _args(), ctx.log)
    return ("stop", why) if why else ("done", "buyers deduped")


def step_unmask(d, ctx) -> tuple[str, str]:
    left = d.unmask_left(MIN_BUYS)
    if not left:
        return "done", "every repeat buyer already had the free check"
    cap = int(S("UNMASK", 60))
    why = _call(d.phase_unmask, _args(limit=cap, map_client=ctx.map_client), ctx.log)
    if why:
        return "stop", why
    after = d.unmask_left(MIN_BUYS)
    return ("more", "free check on %d company(ies), %d left" % (left - after, after)) if after else \
        ("done", "free check on %d company(ies)" % left)


def step_principals(d, ctx) -> tuple[str, str]:
    import enformion_business as eb
    if not eb.is_configured():
        return "done", "the business search has no key on this box (ENFORMION_AP_NAME), so nobody was looked up"
    _clear(d, "principals_plan", "principals")
    why = _call(d.phase_principals, _args(commit=False), ctx.log)     # always plan first: what would it buy
    plan = (d.state().get("principals_plan") or {})
    todo = int(plan.get("todo") or 0)
    if why:
        return "stop", why
    if not todo:
        return "done", "no repeat buyer is waiting for the business search"
    ctx.would_usd += todo * PAID["principals"]
    if not ctx.live:
        return "done", "the business search would look up %d company(ies), up to $%.2f (plan: not bought)" % (
            todo, todo * PAID["principals"])
    n = int(ctx.money.remaining() // PAID["principals"])
    if n < 1:
        return "partial", "%d company(ies) wait for the business search (no money left tonight)" % todo
    why = _call(d.phase_principals, _args(commit=True, limit=min(n, todo)), ctx.log)
    got = d.state().get("principals") or {}
    usd = float(got.get("usd") or 0)
    ctx.money.charge(usd, "buyer registry: business search x%d" % int(got.get("billed") or 0))
    if why:
        return "stop", why
    return ("partial" if todo > n else "done"), "business search on %d company(ies), %d named, $%.2f" % (
        int(got.get("attempted") or 0), int(got.get("resolved") or 0), usd)


def step_profiles(d, ctx) -> tuple[str, str]:
    why = _call(d.phase_profiles, _args(), ctx.log)
    return ("stop", why) if why else ("done", "profiles rebuilt")


def step_crm_phones(d, ctx) -> tuple[str, str]:
    cap = int(S("CRM_READS", 150))
    _clear(d, "crm_phones")
    why = _call(d.phase_crm_phones, _args(limit=cap, crm=ctx.reader, skip_read_days=int(S("EVERY_DAYS", 30))), ctx.log)
    if why:
        return "stop", why
    got = d.state().get("crm_phones") or {}
    left = int(got.get("left_over") or 0)
    return ("more", "read %d record(s), %d left" % (int(got.get("records") or 0), left)) if left else \
        ("done", "read %d record(s) for numbers already on file" % int(got.get("records") or 0))


def _trace_rows(d) -> list[dict]:
    p = Path(d.OUT) / "smartskip_input.csv"
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r.get("buyer_key") and r.get("First Name") and r.get("Last Name")]


def step_trace(d, ctx) -> tuple[str, str]:
    why = _call(d.phase_skipinput, _args(), ctx.log)
    if why and "no traceable" not in why:
        return "stop", why
    rows = [] if why else _trace_rows(d)
    if not rows:
        return "done", "no repeat buyer is waiting for a skip trace"
    ctx.would_usd += len(rows) * PAID["trace"]
    if not ctx.live:
        return "done", "SmartSkip would trace %d buyer(s), up to $%.2f (plan: not bought)" % (len(rows), len(rows) * PAID["trace"])
    n = int(ctx.money.remaining() // PAID["trace"])
    if n < 1:
        return "partial", "%d buyer(s) wait for a skip trace (no money left tonight)" % len(rows)
    from ..autopilot.sources import SmartSkip, Subject
    src = ctx.trace_source or SmartSkip()
    if not src.configured():
        return "done", "SmartSkip has no login on this box (SMARTSKIP_EMAIL), so nobody was traced"
    batch = rows[:n]
    subjects = [Subject(record_uuid=r["buyer_key"], owner_uuid="", kind="person", first=r["First Name"], last=r["Last Name"],
                        street=r["Mailing Address"], city=r.get("Mailing City") or "", state=r.get("Mailing State") or "",
                        zip=r.get("Mailing Zip") or "", mail_street=r["Mailing Address"], mail_city=r.get("Mailing City") or "",
                        mail_state=r.get("Mailing State") or "", mail_zip=r.get("Mailing Zip") or "") for r in batch]
    try:
        found, billed = src.trace(subjects)
    except Exception as exc:  # noqa: BLE001  (a paid order that failed is said; nothing is merged from it)
        return "stop", "SmartSkip failed, nothing merged: %s" % str(exc)[:160]
    ctx.money.charge(float(billed or 0), "buyer registry: SmartSkip x%d" % len(subjects))
    phones = d._load(Path(d.OUT) / "phones.json", {})
    added = 0
    for r in batch:
        nums = found.get(r["buyer_key"]) or []
        if not nums:
            continue
        e = phones.setdefault(r["buyer_key"], {"person": "%s %s" % (r["First Name"], r["Last Name"]), "phones": [],
                                                 "deceased_flag": False, "relatives": 0})
        have = {p["number"] for p in e["phones"]}
        for n_ in nums:
            if n_["number"] not in have:
                e["phones"].append({"number": n_["number"], "type": n_.get("type") or "", "source": "smartskip"})
                have.add(n_["number"])
                added += 1
    d._save(Path(d.OUT) / "phones.json", phones)
    left = len(rows) - len(batch)
    return ("partial" if left else "done"), "SmartSkip traced %d buyer(s), %d new number(s), $%.2f%s" % (
        len(batch), added, float(billed or 0), ("; %d wait for tomorrow" % left) if left else "")


def step_score(d, ctx) -> tuple[str, str]:
    phones = d._load(Path(d.OUT) / "phones.json", {})
    nums = sorted({p["number"] for v in phones.values() for p in v.get("phones") or []})
    cache_path = Path(d.OUT) / "phone_scores.json"
    cache = d._load(cache_path, {})
    # A number the autopilot already scored is not bought again: its score is copied in for free.
    copied = 0
    for n in nums:
        if n not in cache:
            c = ledger.phone(n)
            if c and c.get("tier"):
                cache[n] = {"score": c.get("score"), "tier": c["tier"], "litigator": bool(c.get("litigator")),
                            "line_type": c.get("line_type") or "", "from": "autopilot cache"}
                copied += 1
    if copied:
        d._save(cache_path, cache)
    todo = [n for n in nums if n not in cache]
    if not todo:
        return "done", "every number already scored" + (" (%d from the autopilot's cache)" % copied if copied else "")
    ctx.would_usd += len(todo) * PAID["score"]
    if not ctx.live:
        return "done", "Trestle would score %d number(s), about $%.2f (plan: not bought)" % (len(todo), len(todo) * PAID["score"])
    n = int(ctx.money.remaining() // PAID["score"])
    if n < 1:
        return "partial", "%d number(s) wait for a score (no money left tonight)" % len(todo)
    from ..autopilot.sources import Scorer
    batch = todo[:min(n, len(todo))]
    _clear(d, "score_registry")
    why = _call(d.phase_score, _args(commit=True, scorer=ctx.scorer or Scorer(), score_limit=len(batch)), ctx.log)
    got = d.state().get("score_registry") or {}
    newly = int(got.get("newly_scored") or 0)
    ctx.money.charge(round(newly * PAID["score"], 4), "buyer registry: Trestle x%d" % newly)
    # Shared with the autopilot's cache, so a stage never buys these again. Only numbers scored
    # just now, with the litigator check: an older registry score was taken without it, and a cached
    # tier is trusted as if the check had run.
    after = d._load(cache_path, {}) or {}
    for num in batch:
        c = after.get(num) or {}
        if c.get("tier") and "litigator" in c and not ledger.phone(num):
            ledger.set_phone(num, c.get("score"), c["tier"], c.get("line_type") or "", bool(c.get("litigator")))
    if why:
        return "stop", why
    left = len(todo) - newly
    return ("partial" if left > 0 else "done"), "Trestle scored %d number(s), $%.2f%s" % (
        newly, newly * PAID["score"], ("; %d wait for tomorrow" % left) if left > 0 else "")


STEP_FN = {"sweep": step_sweep, "recent": step_recent, "hydrate": step_hydrate, "aggregate": step_aggregate,
           "unmask": step_unmask, "principals": step_principals, "profiles": step_profiles,
           "crm_phones": step_crm_phones, "trace": step_trace, "score": step_score}


# ------------------------------------------------------------------------- the job
def run(task: dict, *, dispo=None, crm=None, map_client=None, search_client=None, detail_client=None, scorer=None,
        trace_source=None, now: datetime | None = None, clock=time.monotonic) -> dict:
    now = now or datetime.now(timezone.utc)
    if dispo is None:
        import dispo_buyers as dispo
    dispo.use_dir(registry_dir())
    forced = not (task.get("source") or "").startswith("schedule")
    job = _load_job(dispo)
    every = timedelta(days=S("EVERY_DAYS", 30))
    try:
        last = datetime.fromisoformat(job["finished"]) if job.get("finished") else None
    except ValueError:
        last = None
    if last and not forced and now - last < every:
        return {"ok": True, "quiet": True, "head": "Next refresh is due %s." % (last + every).date().isoformat(),
                "live": config.live()}
    if not job.get("started") or job.get("finished"):
        job = {"started": now.isoformat(timespec="seconds"), "finished": "", "done": [], "nights": 0, "notes": {}}
    job["nights"] = int(job.get("nights") or 0) + 1
    if map_client is None:
        import buyer_sweep
        map_client = buyer_sweep.MapClient(min_interval=S("MAP_GAP_S", 0.75))
    ctx = SimpleNamespace(live=config.live(), log=[], would_usd=0.0,
                          money=common.Money(int(task.get("id") or 0), S("RUN_USD", 5.0)),
                          search_client=search_client, detail_client=detail_client, map_client=map_client,
                          reader=_Reader(crm or common.default_crm()), scorer=scorer, trace_source=trace_source)
    t0, budget = clock(), S("MINUTES", 15) * 60
    tonight, problems = [], []
    for step in STEPS:
        if step in job["done"]:
            continue
        if clock() - t0 > budget:
            tonight.append(("time", "out of time tonight; the next run resumes at %s" % step))
            break
        try:
            status, note = STEP_FN[step](dispo, ctx)
        except Exception as exc:  # noqa: BLE001  (one step failing is said; the job resumes there next night)
            status, note = "stop", "%s: %s" % (type(exc).__name__, str(exc)[:200])
        job["notes"][step] = note
        tonight.append((step, note))
        if status == "done":
            job["done"].append(step)
        elif status == "partial":
            continue                    # a paid step out of money does not hold up the free steps after it
        else:
            if status == "stop":
                problems.append("%s: %s" % (step, note))
            break
    finished = all(s in job["done"] for s in STEPS)
    if finished:
        job["finished"] = now.isoformat(timespec="seconds")
    _save_job(dispo, job)
    files = common.files_dir(task, WORKFLOW)
    name = "buyer_registry_%s.log" % (task.get("id") or 0)
    (files / name).write_text("\n".join("%s: %s" % s for s in tonight) + "\n\n" + "\n".join(ctx.log), encoding="utf-8")
    head = ("Refresh finished (started %s, %d night(s))." % (job["started"][:10], job["nights"]) if finished else
            "Refresh under way: %d of %d steps done." % (len(job["done"]), len(STEPS)))
    return {"ok": True, "live": ctx.live, "head": head, "tonight": tonight, "problems": problems, "finished": finished,
            "usd": round(ctx.money.spent, 2), "would_usd": round(ctx.would_usd, 2), "refused": ctx.money.refused,
            "dir": str(dispo.OUT), "file": name, "run_dir": str(files.parent), "forced": forced}


def render(task: dict, result: dict) -> str:
    if not result.get("ok") or result.get("quiet") and not result.get("forced"):
        return result.get("say") or ""
    if result.get("quiet"):
        return ":white_check_mark: *buyer registry* %s" % result.get("head")
    # A scheduled night that only moved a capped step forward is not news; a finish, a problem or an
    # ask from Slack is.
    if not result.get("forced") and not result.get("finished") and not result.get("problems"):
        return ""
    plan = "" if result.get("live") else " _(plan: nothing bought)_"
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    out = ["%s *buyer registry*%s %s" % (icon, plan, result.get("head") or "")]
    for step, note in (result.get("tonight") or [])[:8]:
        out.append("  %s: %s" % (step, note))
    if result.get("usd"):
        out.append("  Spent $%.2f tonight." % result["usd"])
    elif result.get("would_usd") and not result.get("live"):
        out.append("  The paid steps would cost up to $%.2f." % result["would_usd"])
    for p in (result.get("problems") or [])[:3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)
