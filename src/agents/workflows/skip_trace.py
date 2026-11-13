"""skip_trace: phones for one record or a small preset, through the autopilot's own waterfall.

Diggs runs it when asked in Slack ("skip trace 12 Example St", or a preset by its exact title).
It is not a second skip trace engine: it builds the autopilot's retrace stage on the spot, with
the settings of the playbook's own retrace stage (AGENTS_SKIP_TRACE_LIKE, default ftm_retrace),
and hands it only the records this ask named. So the rules are the stage's rules, word for word:
the sources in the playbook's order, a record counts as REACHED only when a Dial First or Dial
Second number lands, every new number is scored before it is written and lands once, tagged with
its source, through the documented upsert-phones route (never the browser wizard), the owner's
skip trace counter is written as the new TOTAL, a litigator's number suppresses every record the
owner holds, and every paid call checks the caps first.

What the ask adds in front of the stage, because a person named the records instead of a preset
that only holds no-number records:
  * a record the gate holds (Do Not Market, a litigator, an opt-out, a sold house, a lead) is
    never sent to a paid source;
  * a dead owner or a probate record is never traced blind: it is named as a deep prospect's job
    and nothing is queued for it (research spends real money, so a person asks for it);
  * a record that already has a callable number is not paid for again;
  * a record with no full name or no mailing address is not traced (a trace on half a name bills
    for a stranger's numbers); a person fixes it first;
  * a company or trust owner goes to the principal finder (entity_principals) on this thread.
And after it, on a live box, every change the run made is read back from the record.

Money: a paid source runs only on a live box (config.live()), inside AGENTS_SKIP_TRACE_RUN_USD for
this one run, the autopilot's data cap, the brain's daily cap and the per-record lifetime cap.

This module also holds the plumbing the other people workflows share (phone_score uses all of
it, entity_principals and buyer_registry use Money and files_dir): one copy, so the four cannot
drift into four slightly different ideas of a cap.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, store
from ..autopilot import budget, gate, ledger, stages, windows
from ..autopilot import policy as policy_mod
from ..autopilot.sources import digits, missing_for_trace, subject_of

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

log = logging.getLogger(__name__)

WORKFLOW = "skip_trace"
SCOPE = "this request"          # the one preset title the scoped view answers for
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
READ_BACK_MAX = 10              # records a run reads back after it writes (requests are budgeted)

# The retrace settings a workflow keeps from the playbook stage it copies. Everything about WHICH
# records (presets, lanes, tranches, look-backs, schedules) is the stage's business, not an ask's.
RETRACE_KEEP = ("sources", "callable_tiers", "agreement_tag", "write_tiers", "reserve_tiers", "keep_numbers",
                "drop_status", "write_line_type", "new_numbers_tag", "count_as_traced", "principals_per_day")


# ------------------------------------------------------------------------- settings
def setting(name: str, default: float) -> float:
    """A number from the environment, read when it is needed (a test or an operator can change it
    without a restart), falling back to the default on anything unreadable."""
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def preset_max() -> int:
    return max(1, int(setting("AGENTS_WF_PRESET_MAX", 25)))


# ------------------------------------------------------------------------- task plumbing
def inputs_of(task: dict) -> dict:
    return dict(store.payload_of(task).get("inputs") or {})


def details_of(task: dict) -> str:
    return str(store.payload_of(task).get("details") or "")


def files_dir(task: dict, workflow: str) -> Path:
    """Where a workflow keeps what it made: config.RUNS_DIR/<workflow>-<task id>/files. The brain's
    /files route serves from there when the result carries run_dir (the folder's parent)."""
    d = Path(config.RUNS_DIR) / ("%s-%s" % (workflow, task.get("id") or 0)) / "files"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thread_enqueue(task: dict, workflow: str):
    """store.enqueue with this task's thread and Slack thread, so a handoff the stage makes (a
    company to the principal finder) answers under the same message the person is watching."""
    def enq(agent, wf, record_uuid="", subject="", **_kw):
        return store.enqueue(agent, wf, record_uuid, subject, source="workflow:%s" % workflow,
                             payload={"requested_by": "%s workflow" % workflow.replace("_", " ")},
                             thread_id=task.get("thread_id") or "", parent_id=int(task.get("id") or 0),
                             hop=int(task.get("hop") or 0), slack_channel=task.get("slack_channel") or "",
                             slack_thread=task.get("slack_thread") or "")
    return enq


def load_policy():
    """The playbook, for the gate's tags, the caps and the stage settings a workflow copies. A
    playbook that will not load must not stop an ask: the gate's defaults and the default caps
    apply, and the result says so."""
    note = ""
    try:
        pol = policy_mod.load()
    except Exception as exc:  # noqa: BLE001
        pol, note = None, "the playbook did not load (%s); the gate's defaults applied" % str(exc)[:120]
    if pol is None:
        pol = policy_mod.Policy(tz="America/New_York", hours=(540, 1080), days=frozenset(range(5)), daily_usd=25.0,
                                record_usd=1.0, callers=[], stages={}, gate=gate.GateConfig(), lanes=None)
    return pol, note


def base_params(pol, stage_id: str, keep: tuple) -> dict:
    st = (getattr(pol, "stages", None) or {}).get(stage_id)
    return {k: st.params[k] for k in keep if st is not None and k in st.params}


def default_crm():
    from ..autopilot.crm import CRM
    return CRM()


# ------------------------------------------------------------------------- money
class Money:
    """A run's spending, for workflows that call a vendor directly (the principal finder, the buyer
    registry). Two caps, both checked BEFORE the call: this run's own (a setting, default small)
    and the brain's daily cap, which every workflow and stage shares. Nothing is spent unless the
    box is live; in dry mode `allows` still answers, so a plan can say what it would cost."""

    def __init__(self, task_id: int, run_cap: float):
        self.task_id, self.run_cap = int(task_id or 0), float(run_cap)
        self.spent = 0.0
        self.refused: list[str] = []

    def allows(self, usd: float, what: str) -> bool:
        day = store.spent(day=store.today(), research=False)
        if self.spent + usd > self.run_cap + 1e-9:
            self.refused.append("%s not paid for: about $%.2f would pass this run's cap of $%.2f" % (what, usd, self.run_cap))
            return False
        if day + usd > config.DAILY_CAP_USD + 1e-9:
            self.refused.append("%s not paid for: about $%.2f would pass the brain's $%.0f a day" % (what, usd, config.DAILY_CAP_USD))
            return False
        return True

    def charge(self, usd: float, what: str) -> None:
        if usd > 0:
            self.spent = round(self.spent + usd, 4)
            store.add_spend(self.task_id, usd, what)

    def remaining(self) -> float:
        """What a batch step may still spend right now: the smaller of this run's cap and what is
        left of the brain's day. A batch is sized from this BEFORE it is sent, never after."""
        day = store.spent(day=store.today(), research=False)
        return max(0.0, min(self.run_cap - self.spent, config.DAILY_CAP_USD - day))


# ------------------------------------------------------------------------- which records
def resolve_target(task: dict, crm, now: datetime) -> dict:
    """{"kind": "record", "uuid"} or {"kind": "preset", "title", "must"} or {"kind": "", "say"}.

    A record the workflow tool already resolved arrives as the task's record. Otherwise the ask's
    `target` is a record uuid, the exact title of a saved preset (case does not matter), or a street
    line that matches exactly one record. Two matches are never guessed between."""
    if task.get("record_uuid"):
        return {"kind": "record", "uuid": task["record_uuid"]}
    inp = inputs_of(task)
    text = str(inp.get("target") or inp.get("record") or inp.get("preset") or "").strip()
    if not text:
        return {"kind": "", "say": "Name one record (its uuid or street line) or one saved preset by its exact title."}
    if UUID_RE.match(text):
        return {"kind": "record", "uuid": text}
    titles = {t.lower(): t for t in (crm.presets() or {})}
    if text.lower() in titles:
        title = titles[text.lower()]
        must, notes = windows.resolve(crm.preset_must(title), now)
        return {"kind": "preset", "title": title, "must": must, "notes": [n for n in notes if "dropped" in n]}
    _, hits = crm.search({"property_type": "clean", "search": "address_prefix:%s" % text}, limit=5)
    hits = [h for h in hits or [] if h.get("uuid")]
    if len(hits) == 1:
        return {"kind": "record", "uuid": hits[0]["uuid"]}
    if hits:
        return {"kind": "", "say": "Several records match %r; name one by its uuid: %s" % (
            text, "; ".join("%s %s" % (stages.addr(h), h["uuid"]) for h in hits[:5]))}
    return {"kind": "", "say": "No record and no preset in this account matches %r." % text}


def record_row(crm, uuid_: str) -> tuple[dict, dict]:
    """(full record, its search row). The row matters: DataSift's do-not-call registry flag exists
    ONLY on a search row, so a row is found through the one filter proven to return exactly an
    owner's records (`owners`), and a row built from the record stands in only when that fails."""
    rec = crm.record(uuid_) or {}
    row = None
    owner = (rec.get("owner") or {}).get("uuid") or ""
    if owner:
        try:
            row = next((r for r in crm.owner_records(owner) if r.get("uuid") == uuid_), None)
        except Exception:  # noqa: BLE001  (a missing row only loses the registry flag; the gate still runs)
            row = None
    return rec, row or {"uuid": uuid_, "status": rec.get("status"), "owner": {"uuid": owner}}


class Scoped:
    """The account as one ask's stage sees it: the real client for every read and write, but the
    only records the stage is handed are the ones this ask named. A record the workflow already read
    to decide whether to send it is served to the stage once, so it is not read twice; any later
    read (a read-back) goes to DataSift."""

    def __init__(self, crm, rows: list, records: dict | None = None):
        self._crm = crm
        self._rows = list(rows)
        self._first = dict(records or {})

    def __getattr__(self, name):
        return getattr(self._crm, name)

    def presets(self):
        return {SCOPE: {"uuid": "", "folder": "", "must": {"__scope__": SCOPE}}}

    def preset_must(self, title):
        if title == SCOPE:
            return {"__scope__": SCOPE}
        return self._crm.preset_must(title)

    def rows(self, must, cap=10000):
        if (must or {}).get("__scope__") == SCOPE:
            return list(self._rows)[:cap]
        return self._crm.rows(must, cap=cap)

    def record(self, uuid_):
        if uuid_ in self._first:
            return self._first.pop(uuid_)
        return self._crm.record(uuid_)


def scope_rows(task: dict, crm, now: datetime, target: dict) -> tuple[list, dict, dict]:
    """(rows, records already read, facts about the scope). A preset is cut to AGENTS_WF_PRESET_MAX
    records: the autopilot's stages work whole lanes, an ask works a handful."""
    if target["kind"] == "record":
        rec, row = record_row(crm, target["uuid"])
        return [row], {target["uuid"]: rec}, {"scope": "record", "street": stages.addr(rec)}
    cap = preset_max()
    total = crm.count(target["must"])
    rows = [r for r in crm.rows(target["must"], cap=cap) if r.get("uuid")]
    return rows, {}, {"scope": "preset", "title": target["title"], "preset_total": total, "taken": len(rows),
                      "notes": target.get("notes") or []}


# ------------------------------------------------------------------------- the stage
def stage_ctx(task: dict, pol, crm, *, kind: str, stage_id: str, params: dict, run_cap: float,
              now: datetime, sources=None, scorer=None, enqueue=None) -> stages.Ctx:
    """A Ctx for one ask: the stage `kind` with the given settings, live only when the box is.

    The run's own cap rides on the stage's daily cap: what this workflow already spent today plus
    the run cap, so this run can spend at most the run cap whatever ran before it, and every other
    cap (the autopilot's data cap, the brain's daily cap, the per-record lifetime cap) is checked by
    the stage exactly as it is for the autopilot."""
    day = ledger.day(now, pol.tz)
    p = dict(params)
    p["daily_usd"] = round(ledger.spent(day, stage_id) + float(run_cap), 4)
    live = config.live()
    st = policy_mod.Stage(id=stage_id, kind=kind, owner="research", mode="live" if live else "plan",
                          summary="", schedule=[], presets=[SCOPE], params=p)
    out = stages.Summary(stage=stage_id, kind=kind, owner="research", mode=st.mode, live=live)
    return stages.Ctx(policy=pol, stage=st, crm=crm, run_id="%s-%s" % (stage_id, uuid.uuid4().hex[:8]), now=now,
                      live=live, out=out, sources=sources or {}, scorer=scorer, task_id=int(task.get("id") or 0),
                      enqueue=enqueue)


def run_stage(ctx: stages.Ctx, fn) -> None:
    try:
        fn(ctx)
    except budget.BudgetExhausted as exc:
        ctx.out.problem("stopped early: %s" % exc)


def read_back(crm, run_id: str, limit: int = READ_BACK_MAX) -> tuple[int, list[str]]:
    """Read every record this run changed (up to `limit`) and check the change took, from the run's
    own change log: the numbers it added are on the owner, the counter is at least the total it
    wrote, and a tiered phone carries exactly one tier. Returns (records checked, problems)."""
    by_rec: dict = {}
    for c in ledger.changes(run_id=run_id):
        by_rec.setdefault(c["record_uuid"], []).append(c)
    problems, checked = [], 0
    for u, cs in list(by_rec.items())[:limit]:
        rec = crm.record(u) or {}
        checked += 1
        ow = rec.get("owner") or {}
        phones = {digits(p.get("number")): p for p in ow.get("phones") or []}
        street = stages.addr(rec)
        for c in cs:
            after = c.get("after")
            if c["what"] == "add_numbers":
                missing = [n for n in (after or []) if digits(n) not in phones]
                if missing:
                    problems.append("%s: %d new number(s) did not read back on the owner" % (street, len(missing)))
            elif c["what"] == "skiptrace_attempts":
                if int(ow.get("skiptrace_attempts") or 0) < int(after or 0):
                    problems.append("%s: the skip trace counter reads %s, not %s" % (street, ow.get("skiptrace_attempts"), after))
            elif c["what"] == "phones":
                for n in after or []:
                    p = phones.get(digits(n))
                    tiers = [t for t in stages.tag_titles(p or {}) if t in stages.TIER_NAMES]
                    if p is None or len(tiers) != 1:
                        problems.append("%s: a scored number does not carry exactly one tier" % street)
                        break
    return checked, problems


# ------------------------------------------------------------------------- the waterfall
def screen(rec: dict, row: dict, pol, sources_order: list) -> tuple[str, str, dict]:
    """('trace' | 'principal' | '', why, the record to hand the stage) for one record. Only
    'trace' and 'principal' go to the stage; anything else is reported with its reason and costs
    nothing. A company whose principal is already named on the record is handed over AS that person
    (entity_principals.as_principal_record), so the stage traces the person and the numbers land on
    the company's owner."""
    if not rec or not rec.get("uuid"):
        return "", "the record could not be read", rec
    if stages.status_of(rec) in stages.DEAD_STATUSES:
        return "", "status %s, finished" % stages.status_of(rec), rec
    cfg = getattr(pol, "gate", None) or gate.GateConfig()
    subj = subject_of(rec)
    if subj.kind in ("deceased", "probate"):
        return "", ("a probate record" if subj.kind == "probate" else "the owner may have died") + \
            ": that is a deep prospect, never a blind trace (ask Diggs for one)", rec
    v = gate.check(rec, "research" if subj.org else "trace", cfg=cfg, row=row)
    if not v.ok:
        if v.code == "deceased":
            return "", "the owner may have died (%s): that is a deep prospect, never a blind trace" % v.reasons[0][1], rec
        return "", "held back: %s" % v.reason(), rec
    if subj.org:
        from .entity_principals import as_principal_record    # here, not at the top: that module imports this one
        named = as_principal_record(rec)
        if named is None:
            return "principal", "a company or trust owns it", rec
        vt = gate.check(rec, "trace", cfg=cfg, row=row)      # tracing a person is a trace, whoever owns the deed
        if not vt.ok:
            return "", "held back: %s" % vt.reason(), rec
        rec, subj = named, subject_of(named)
    missing = missing_for_trace(subj)
    if missing:
        return "", "%s: a person fixes the record before a paid trace" % " and ".join(missing), rec
    if gate.usable_numbers(rec, "call", cfg, row):
        return "", "already has a callable number, nothing bought", rec
    tried = ledger.tried_sources(rec["uuid"])
    if sources_order and set(sources_order) <= tried:
        return "", "every source has already been tried on it", rec
    return "trace", "", rec


def run(task: dict, *, crm=None, pol=None, sources=None, scorer=None, now: datetime | None = None,
        enqueue=None) -> dict:
    now = now or datetime.now(timezone.utc)
    if pol is None:
        pol, note = load_policy()
    else:
        note = ""
    crm = crm or default_crm()
    target = resolve_target(task, crm, now)
    if not target["kind"]:
        return {"ok": False, "say": target["say"]}
    rows, first, scope = scope_rows(task, crm, now, target)
    params = {"sources": ["smartskip", "tracerfy"]}
    params.update(base_params(pol, os.environ.get("AGENTS_SKIP_TRACE_LIKE") or "ftm_retrace", RETRACE_KEEP))
    # The ask's own rules over the copied ones: a dead owner and a company are decided above, a
    # miss is reported (never tagged Mail Only on a person's say-so), and no look-back runs here.
    params.update({"deceased": "deep_prospect", "entity": "principals", "after_sources": "none",
                   "max_records": max(1, len(rows)), "max_scan": max(1, len(rows))})
    order = [str(s) for s in params.get("sources") or []]
    held, keep, records = [], [], {}
    for row in rows:
        u = row["uuid"]
        rec = first.pop(u, None) or crm.record(u)
        verdict, why, use = screen(rec, row, pol, order)
        if verdict:
            keep.append(row)
            records[u] = use
        else:
            held.append({"record": u, "street": stages.addr(rec) if rec else u[:8], "why": why})
    result = {"ok": True, "live": config.live(), **scope, "held": held, "sources": order,
              "run_cap_usd": setting("AGENTS_SKIP_TRACE_RUN_USD", 2.0)}
    # A saved date window the search cannot read is dropped from the preset: said, because the
    # preset then holds more records than its title promises.
    problems = ([note] if note else []) + ["%s: %s" % (scope.get("title"), n) for n in scope.get("notes") or []]
    if problems:
        result["problems"] = problems
    if not keep:
        result.update(head="Nothing to trace.", acted=0, usd=0.0, would_usd=0.0)
        return result
    from ..autopilot.sources import Scorer, default_sources
    ctx = stage_ctx(task, pol, Scoped(crm, keep, records), kind="retrace", stage_id=WORKFLOW, params=params,
                    run_cap=result["run_cap_usd"], now=now, sources=sources if sources is not None else default_sources(),
                    scorer=scorer if scorer is not None else Scorer(), enqueue=enqueue or thread_enqueue(task, WORKFLOW))
    run_stage(ctx, stages.run_retrace)
    out = ctx.out
    checked, rb = read_back(crm, ctx.run_id) if ctx.live else (0, [])
    result.update(head=out.head, lines=out.lines, problems=list(result.get("problems") or []) + out.problems + rb,
                  tally=dict(out.tally), acted=out.acted, usd=round(out.usd, 2), would_usd=round(out.would_usd, 2),
                  writes=out.writes, run_id=ctx.run_id, read_back=checked)
    return result


# ------------------------------------------------------------------------- Slack
def render_people(title: str, result: dict) -> str:
    """The Slack text for a people workflow: the head, a few lines, what was held and why, and any
    problem. No phone number ever appears (the stage's lines carry none; held reasons carry none)."""
    if not result.get("ok"):
        return result.get("say") or ""
    plan = "" if result.get("live") else " _(plan: nothing written or bought)_"
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    scope = result.get("street") or result.get("title") or ""
    out = ["%s *%s* %s%s %s" % (icon, title, scope, plan, result.get("head") or "")]
    if result.get("scope") == "preset" and result.get("preset_total", 0) > result.get("taken", 0):
        out.append("  The preset holds %d record(s); this ask took the first %d (the autopilot's stages work whole lanes)."
                   % (result["preset_total"], result["taken"]))
    for ln in (result.get("lines") or [])[:5]:
        out.append("  %s" % ln)
    for h in (result.get("held") or [])[:5]:
        out.append("  %s: %s" % (h["street"], h["why"]))
    if len(result.get("held") or []) > 5:
        out.append("  ... and %d more held back" % (len(result["held"]) - 5))
    for p in (result.get("problems") or [])[:3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)


def render(task: dict, result: dict) -> str:
    return render_people("skip trace", result)
