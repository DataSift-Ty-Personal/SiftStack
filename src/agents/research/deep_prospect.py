"""Research agent workflow: deep prospect one record, end to end, with no person in the loop.

Fired when a record's status changes to "Deep Prospecting". Runs the single-record v5 chain
(deep_prospect_record + dream100_dp) with the researcher in place of the person who used to write
the research and analysis files:

    records -> smartskip -> merge -> RESEARCH -> ANALYSIS -> heirs -> merge -> score -> gapfill
            -> verify -> FINAL ANALYSIS -> build -> writeback

Every paid phase checks the run cap and the daily cap first and is skipped (not failed) when it
would cross either, so a run still ends with a property file built from what it has. In dry mode
no vendor is paid and nothing is written to the CRM; the research still runs and the pack still
posts, so the team can judge the output before the agent spends money.

The run folder lives on the volume and is keyed by address, so a re-run reuses every paid
SmartSkip order instead of buying it again.

The order above is today's, and the default. AGENTS_DP_ORDER=free_first (build plan 3.3) reads what
is free first and pays for a trace only afterwards:

    records -> FREE LOOK (the notice text on the record, the county obituary index, a saved deed
            lookup) -> RESEARCH -> smartskip -> merge -> ANALYSIS -> heirs -> ... as above

A free look that shows the owner no longer owns the house ends the run there, before a cent is
spent. What may be written back at the end is decided in writeback.py.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .. import config, slack, store
from . import open_research, researcher, writeback

log = logging.getLogger(__name__)

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REAL_CONTACT_USD = float(os.environ.get("AGENTS_REAL_CONTACT_USD") or 0.03)


class Budget:
    def __init__(self, task_id: int):
        self.task_id = task_id
        self.skipped: list[str] = []

    def allows(self, usd: float, what: str, reserve: float = 0.0) -> bool:
        """May this step spend about `usd`? `reserve` is what must still be left in the run
        afterwards (the final analysis). The day is counted against research's own budget."""
        day = datetime.now(timezone.utc).isoformat()[:10]
        run_total = store.spent(task_id=self.task_id) + usd + reserve
        day_total = store.spent(day=day, research=True) + usd
        if run_total > config.RUN_CAP_USD or day_total > config.RESEARCH_DAILY_CAP_USD:
            self.skipped.append("%s skipped: about $%.2f would pass the cap (run $%.0f%s, research day $%.0f)"
                                % (what, usd, config.RUN_CAP_USD,
                                   " with $%.2f held for the final analysis" % reserve if reserve else "",
                                   config.RESEARCH_DAILY_CAP_USD))
            return False
        return True

    def charge(self, usd: float, what: str):
        store.add_spend(self.task_id, usd, what)


def _ledger_total(run) -> float:
    return sum(float(v.get("usd") or 0) for v in (run.load("ledger.json", {}) or {}).values())


def _phase(name: str, fn, log_lines: list[str]):
    """Run one phase, capture what it prints, and turn a STOP gate into a readable error."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
        ok, err = True, ""
    except SystemExit as exc:      # dream100_dp.gate raises SystemExit("STOP: ...")
        ok, err = False, str(exc)
    except Exception as exc:       # noqa: BLE001
        ok, err = False, "%s: %s" % (type(exc).__name__, str(exc)[:300])
    out = buf.getvalue().strip()
    log_lines.append("== %s %s%s" % (name, "ok" if ok else "FAILED", (" | " + err) if err else ""))
    if out:
        log_lines.extend(out.splitlines()[-25:])
    return ok, err


def _free_look(rn, row: dict, cfg: dict, slug: str) -> dict:
    """Gather what is free about the record and leave it where the research brief reads it. The
    obituary index answers go through the record's own web cache, so the research engine that
    runs next does not ask the library for the same name again."""
    x = (rn.load("records.json") or [{}])[0]
    web = open_research.Web(rn.p("research", slug + "_webcache.json"))
    look = researcher.free_look(x, row, lookup=web.obituary_index,
                                deed=researcher.saved_deed(rn, row.get("uuid") or "", cfg["deed_dir"]))
    web.save()
    rn.save("free_look.json", look)
    return look


def _look_line(look: dict) -> str:
    idx = look.get("obituary_index") or {}
    hits = sum(len(x.get("possible") or []) for x in idx.get("lookups") or [])
    return "notice text %d characters%s; obituary index %s; deed lookup %s" % (
        len(look.get("notice_text") or ""),
        ", mentions %s" % ", ".join(look["notice_mentions"][:3]) if look.get("notice_mentions") else "",
        ("%d possible row(s)" % hits) if idx.get("lookups") else ("not asked" if idx.get("covered") else "none for this county"),
        "on file" if look.get("deed") else "none saved")


def run(task: dict) -> dict:
    import deep_prospect_record as dpr   # noqa: E402  (imported late: it monkeypatches dream100_dp)
    dp = dpr.dp
    dp.load_env()
    live = config.live()
    cfg = writeback.settings()
    free_first = cfg["order"] == "free_first"
    budget = Budget(task["id"])
    lines: list[str] = []

    row = dpr.row_for(task["record_uuid"])
    dp.gate(bool(row.get("street")), "the record has no street address")
    slug = dp.slug_of(row["street"])
    run_dir = config.RUNS_DIR / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    rn = dp.Run(str(run_dir))                      # absolute path: Path("/app") / "/data/..." is /data/...
    prior = rn.load("row.json") or {}
    row["context"] = prior.get("context") or "Status changed to Deep Prospecting (Research agent)"
    row["subject_address"] = prior.get("subject_address") or "mailing"
    rn.save("row.json", row)
    dpr.CFG["subject_address"] = row["subject_address"]
    dpr.patch(row)
    args = SimpleNamespace(commit=live, top=1, only="", context=row["context"], subject_address="",
                           phase="", run=str(run_dir))

    def paid(name: str, estimate: float, fn) -> bool:
        if not live:
            lines.append("== %s skipped (dry mode: no vendor spend)" % name)
            return False
        if not budget.allows(estimate, name, reserve=config.FINAL_RESERVE_USD):
            lines.append("== " + budget.skipped[-1])
            return False
        before = _ledger_total(rn)
        ok, _ = _phase(name, fn, lines)
        budget.charge(_ledger_total(rn) - before, name)
        return ok

    before = _ledger_total(rn)
    ok, err = _phase("records", lambda: dpr.phase_records(args, rn, row), lines)
    budget.charge(_ledger_total(rn) - before, "records (street photo, call transcription)")
    if not ok:
        raise RuntimeError("records phase failed: " + err)

    def owner_trace():
        owner_rows = dp.smartskip_rows(rn.load("records.json"))
        paid("smartskip", len(owner_rows) * dp.SMARTSKIP_PER_HIT, lambda: dp.phase_smartskip(args, rn))
        if rn.load("smartskip.json") is not None:
            _phase("merge", lambda: dpr.phase_merge(args, rn, row), lines)

    def people_placeholder():
        if rn.load("people.json") is None:
            rn.save("people.json", [{"slug": slug, "people": [], "crm_only": [], "research_loaded": False}])

    if free_first:
        try:
            look = _free_look(rn, row, cfg, slug)
            lines.append("== free look ok (%s)" % _look_line(look))
        except Exception as exc:  # noqa: BLE001  (a free look that fails stops nothing: the run goes on without it)
            look = {}
            lines.append("== free look FAILED | %s: %s" % (type(exc).__name__, str(exc)[:200]))
            if rn.p("free_look.json").exists():
                rn.save("free_look.json", None)    # never an older run's look in place of this one
        if look.get("stop"):
            # Nothing was paid for and nothing is written: the house is no longer this owner's.
            result = {"slug": slug, "street": row["street"], "run_dir": str(run_dir), "pdf": "", "pdf_name": "",
                      "headline": "Stopped before any paid step. " + look["stop"], "owner_status": "",
                      "decision_maker": "", "must_sign": [], "call_order": [], "open_items": [],
                      "written_back": False, "live": live, "skipped": budget.skipped,
                      "spent_usd": round(store.spent(task_id=task["id"]), 2), "log": lines[-80:],
                      "stopped": look["stop"], "order": cfg["order"]}
            rn.save("agent_result.json", result)
            return result
    elif rn.p("free_look.json").exists():
        rn.save("free_look.json", None)        # left by an earlier free-first run: today's order reads none
    if not free_first:
        owner_trace()
    people_placeholder()

    # The Claude researcher unless AGENTS_RESEARCH_ENGINE=open (two low-cost models, cross-checked).
    eng = open_research.engine()
    llm_cap = eng.cap
    if budget.allows(llm_cap, "research", reserve=config.FINAL_RESERVE_USD if live else 0.0):
        _, usd = eng.research(rn, row)
        budget.charge(usd, eng.label)
        lines.append("== research ok ($%.2f, %s engine)" % (usd, eng.name))
        if free_first:
            owner_trace()                      # the paid trace comes only after the free facts and the research
        _, usd = eng.analyze(rn, row, final=False)
        budget.charge(usd, "analysis")
        lines.append("== analysis ok ($%.2f)" % usd)
    else:
        # In the free-first order no trace is bought for a record the research could not run on.
        lines.append("== " + budget.skipped[-1])

    heir_rows, _ = (dpr.heir_rows(rn, row) if rn.load("analysis/%s.json" % slug) else ([], []))
    if heir_rows and paid("heirs", len(heir_rows) * dp.SMARTSKIP_PER_HIT, lambda: dpr.phase_heirs(args, rn, row)):
        _phase("merge", lambda: dpr.phase_merge(args, rn, row), lines)

    if live and rn.load("analysis/%s.json" % slug):
        n_score = len(dp.numbers_to_score(rn) or {})
        paid("score", n_score * dp.TRESTLE_PER_NUM, lambda: dp.phase_score(args, rn))
        paid("gapfill", 5 * dp.TRACERFY_PER_REC, lambda: dpr.phase_gapfill(args, rn, row))
        n_verify = len(dpr.verify_pairs(rn, row) or [])
        paid("verify", n_verify * REAL_CONTACT_USD, lambda: dpr.phase_verify(args, rn, row))
        if budget.allows(1.0, "final analysis"):
            _, usd = eng.analyze(rn, row, final=True)
            budget.charge(usd, "final analysis")
            lines.append("== final analysis ok ($%.2f)" % usd)

    built, err = _phase("build", lambda: dpr.phase_build(args, rn, row), lines)
    wrote = False
    if built and live:
        wrote, _ = _phase("writeback", lambda: dpr.phase_writeback(args, rn, row), lines)

    an = rn.load("analysis/%s.json" % slug) or {}
    pdf = rn.p("packs", slug + ".pdf")
    result = {"slug": slug, "street": row["street"], "run_dir": str(run_dir),
              "pdf": str(pdf) if pdf.exists() else "", "pdf_name": pdf.name if pdf.exists() else "",
              "headline": an.get("headline", ""), "owner_status": an.get("owner_status", ""),
              "decision_maker": an.get("decision_maker", ""), "must_sign": an.get("must_sign", []),
              "call_order": an.get("call_order", []), "open_items": an.get("open_items", []),
              "written_back": wrote, "live": live, "skipped": budget.skipped,
              "spent_usd": round(store.spent(task_id=task["id"]), 2), "log": lines[-80:],
              "order": cfg["order"], "research_level": 0, "verified": [], "heir_shares": None, "writeback": ""}
    if not built:
        result["build_error"] = err
    if an:
        # What the result file carries beyond the pack: the level, who is verified, and each heir's
        # share when more than one person signs (an offer sheet reads heir_shares, plan 3.9).
        try:
            d = next(d for d in dp.assemble(rn) if d["slug"] == slug)
            dec = dp.decision(rn, d, cfg)
            result.update(research_level=dec["level"][0], heir_shares=dec["shares"],
                          verified=sorted(n for n, v in dec["verified"].items() if v["verified"]))
        except Exception as exc:  # noqa: BLE001  (the file is still worth having without them)
            lines.append("== level and shares not worked out: %s: %s" % (type(exc).__name__, str(exc)[:200]))
            result["log"] = lines[-80:]
    if wrote:
        result["writeback"] = ((rn.load("writeback_audit.json") or {}).get(slug) or {}).get("summary") or ""
    rn.save("agent_result.json", result)
    return result


def _last4(n: str) -> str:
    d = "".join(ch for ch in (n or "") if ch.isdigit())
    return d[-4:] if d else ""


def slack_text(task: dict, r: dict) -> str:
    lines = [":mag: *Deep prospect: %s*  (Research agent, %s)" % (r["street"], "live" if r["live"] else "dry run")]
    if r.get("headline"):
        lines.append(r["headline"])
    if r.get("owner_status"):
        lines.append("Owner: %s%s" % (r["owner_status"],
                                      ", research level %d" % r["research_level"] if r.get("research_level") else ""))
    signers = [m.get("short") or m.get("who") for m in r.get("must_sign") or [] if m.get("who")]
    if signers:
        lines.append("Who signs: " + "; ".join(signers[:4]))
    shares = r.get("heir_shares") or {}
    if shares.get("computed"):
        lines.append("Shares if there is no will: " + ", ".join("%s %s" % (h["name"], h["fraction"])
                                                                for h in shares["heirs"][:5]))
    elif shares.get("reason"):
        lines.append("Shares not worked out: " + shares["reason"][:180])
    calls = ["%s (ends %s)" % (c.get("who"), _last4(c.get("number"))) for c in (r.get("call_order") or [])[:3]
             if c.get("who")]
    if calls:
        lines.append("Call first: " + ", then ".join(calls))
    if r.get("pdf_name"):
        url = slack.file_url(task["id"], r["pdf_name"])
        lines.append("Property file: %s" % (url or "(built on the box; set AGENTS_PUBLIC_URL for a link)"))
    elif r.get("build_error"):
        lines.append("Property file not built: %s" % r["build_error"][:200])
    tail = "Spent $%.2f" % r.get("spent_usd", 0)
    tail += " | written back to the record" if r.get("written_back") else " | nothing written to the CRM"
    lines.append(tail)
    if r.get("writeback"):
        lines.append("Written: " + r["writeback"])
    for s in (r.get("skipped") or [])[:3]:
        lines.append(":warning: " + s)
    for item in (r.get("open_items") or [])[:2]:
        lines.append("Open: " + item)
    return "\n".join(lines)
