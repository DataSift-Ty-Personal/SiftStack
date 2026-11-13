"""deal_buyers: the top buyers for one contracted house, with the decision maker and their numbers.

src/deal_buyer_match.py (sweep, hydrate, rank: free reads) and src/deal_buyer_dp.py (CRM read and
dossier: free; SmartSkip trace and Trestle Real Contact: paid) run on the brain for one record. The
ranking is the module's own; this file decides where it runs and what it may spend.

It can be started by a DataSift webhook (the under-contract sequence), which hands over a record id
and nothing else, so it is built to be safe that way:
  * a second delivery of the same record inside the dedupe window is not queued again (store.enqueue);
  * every paid step needs a live box, its key on the box, and room under this workflow's run cap
    (AGENTS_DEAL_BUYERS_RUN_CAP_USD) and the brain's day cap; in dry mode it says what each would cost;
  * deed reads are capped per run and per day and taken nearest first: Acceptable use forbids SiftMap
    detail pulls in volume without adding the records, and the sweep can find hundreds of houses;
  * the ask (the price we offer buyers) comes from the request, else a custom field
    (AGENTS_BUYER_ASK_FIELDS, never the seller's asking price); with none, price fit is not scored and
    the result says so instead of inventing a band.

No number reaches Slack: a buyer's line shows how many numbers are on file, and the dial sheet (behind
a signed link) carries them. Nothing is written to DataSift and nobody is contacted.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import threading
from pathlib import Path

from .. import config, store
from .quick_screen import (Budget, MissingKey, count_reads, county_key, crm, deed_reads_left, field_money,
                           file_line, files_dir, inputs_of, locate, money, safe_text, setting, siftmap,
                           subject_of, usd)

NAME = "deal_buyers"


class CountingMap:
    """The SiftMap client with its detail reads counted, so the day's read limit holds across
    every deal workflow. The hydrate phase reads on three threads, hence the lock."""

    def __init__(self, inner):
        self.inner, self.details, self.searches = inner, 0, 0
        self._lock = threading.Lock()

    def search(self, body: dict) -> dict:
        with self._lock:
            self.searches += 1
        return self.inner.search(body)

    def get_detail(self, pid):
        with self._lock:
            self.details += 1
        return self.inner.get_detail(pid)


def quiet(log_lines: list, fn, *args, **kwargs):
    """Run one phase with its printed lines kept for the run log rather than the box's stdout."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return fn(*args, **kwargs)
    finally:
        log_lines.extend(buf.getvalue().splitlines()[-40:])


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _ledger_total(work: Path, key: str) -> float:
    return sum(float(e.get("amount") or 0) for e in _load(work / "ledger.json", []) if e.get("key") == key)


def paid_steps(a, work: Path, budget: Budget, log_lines: list) -> dict:
    """SmartSkip on the named decision makers, then Real Contact on every (person, number) pair, each
    only when the box is live, its key is set and the caps leave room. Nothing else spends here."""
    import deal_buyer_dp as dbp
    res = {"ran": [], "not_run": [], "would_usd": 0.0}
    dossier = _load(work / "dossier.json", [])
    rows, skipped = dbp._trace_targets(dossier, _load(work / "trace.json", {}))
    trace_usd = round(len(rows) * dbp.SMARTSKIP_PER_HIT, 2)
    if skipped:
        res["not_run"].append("%d decision maker(s) not traced: no verified home street" % len(skipped))
    if rows:
        if not config.live():
            res["not_run"].append("SmartSkip on %d decision maker(s), about $%.2f (dry run)" % (len(rows), trace_usd))
            res["would_usd"] += trace_usd
        elif not (os.environ.get("SMARTSKIP_EMAIL") and os.environ.get("SMARTSKIP_PASSWORD")):
            res["not_run"].append("SmartSkip skipped: SMARTSKIP_EMAIL and SMARTSKIP_PASSWORD are not set on this box")
        elif not budget.allows(trace_usd, "SmartSkip"):
            res["not_run"].append(budget.skipped[-1])
        else:
            before = _ledger_total(work, "smartskip")
            a.commit = True
            try:
                quiet(log_lines, dbp.phase_trace, a, work)
                spent = max(0.0, _ledger_total(work, "smartskip") - before)
                res["ran"].append("SmartSkip on %d decision maker(s), $%.2f" % (len(rows), spent))
            except SystemExit as exc:
                # The order may be paid before it fails; the caps count the most it could have cost.
                spent = trace_usd
                res["not_run"].append("SmartSkip stopped: %s" % str(exc)[:160])
            finally:
                a.commit = False
            budget.charge(spent, "SmartSkip x%d" % len(rows))
    cache = _load(work / "verify.json", {})
    pairs = [p for p in dbp._pairs(dossier, _load(work / "traced.json", {})) if "%s|%s" % (p[1], p[2]) not in cache]
    per = float(setting("AGENTS_REAL_CONTACT_USD", 0.03))
    most = int(setting("AGENTS_DEAL_BUYERS_MAX_PAIRS", 60))
    verify_usd = round(len(pairs) * per, 2)
    if pairs:
        key = os.environ.get("TRESTLE_PAID_API_KEY") or os.environ.get("TRESTLE_API_KEY")
        if len(pairs) > most:
            res["not_run"].append("Real Contact skipped: %d pairs is over the cap of %d (AGENTS_DEAL_BUYERS_MAX_PAIRS)"
                                  % (len(pairs), most))
        elif not config.live():
            res["not_run"].append("Real Contact on %d (person, number) pair(s), about $%.2f (dry run)" % (len(pairs), verify_usd))
            res["would_usd"] += verify_usd
        elif not key:
            res["not_run"].append("Real Contact skipped: TRESTLE_PAID_API_KEY or TRESTLE_API_KEY is not set on this box")
        elif not budget.allows(verify_usd, "Real Contact"):
            res["not_run"].append(budget.skipped[-1])
        else:
            a.commit = True
            try:
                quiet(log_lines, dbp.phase_verify, a, work)
                res["ran"].append("Real Contact on %d pair(s), about $%.2f" % (len(pairs), verify_usd))
            except SystemExit as exc:
                res["not_run"].append("Real Contact stopped: %s" % str(exc)[:160])
            finally:
                a.commit = False
            budget.charge(verify_usd, "Real Contact x%d" % len(pairs))
    res["would_usd"] = round(res["would_usd"], 2)
    return res


def match_args(task: dict, subj: dict, ask: float, contract: float) -> argparse.Namespace:
    """The options deal_buyer_match.py and deal_buyer_dp.py parse, for this house."""
    return argparse.Namespace(
        slug="task%d" % int(task.get("id") or 0), phase="all", address=subj["street"],
        subject_id=subj["dataflik_id"], lat=subj["lat"], lon=subj["lon"], beds=subj["beds"] or 3,
        baths=subj["baths"] or 1.0, sqft=float(subj["sqft"]), year=subj["year_built"], ask=float(ask),
        contract=float(contract), comp_mi=float(setting("AGENTS_DEAL_BUYERS_COMP_MI", 1.0)),
        comp_months=int(setting("AGENTS_DEAL_BUYERS_COMP_MONTHS", 18)),
        buyer_mi=float(setting("AGENTS_DEAL_BUYERS_BUYER_MI", 3.0)),
        buyer_months=int(setting("AGENTS_DEAL_BUYERS_BUYER_MONTHS", 36)), band_lo=0.6, band_hi=1.6,
        top=int(setting("AGENTS_DEAL_BUYERS_TOP", 10)), min_buys=3, show=0, county=county_key(subj["county"]),
        detail_cap=min(int(setting("AGENTS_DEAL_BUYERS_DEED_READS", 150)), deed_reads_left()),
        page_size=int(setting("AGENTS_SIFTMAP_PAGE_SIZE", 100)), commit=False, verbose=False)


def run(task: dict) -> dict:
    import deal_buyer_dp as dbp
    import deal_buyer_match as dbm
    inputs, _ = inputs_of(task)
    folder = files_dir(task, NAME)
    work = folder.parent / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = {"ok": False, "run_dir": str(folder.parent), "live": config.live(), "files": []}
    rec, fields, why, _ = locate(task, inputs)
    if not rec:
        out["why"] = why
        return out
    subj = subject_of(rec)
    out["street"] = subj["street"] or rec["uuid"]
    if not (subj["lat"] and subj["lon"]):
        out["why"] = "the record has no map point, so there is no area to search for buyers"
        return out
    if not subj["sqft"]:
        out["why"] = "the record has no square footage, so a buyer's product fit cannot be judged"
        return out
    # The ask here is the price WE offer buyers (the assignment price), never the seller's asking
    # price: a band built on the seller's number would score buyers against the wrong deal.
    ask, ask_from = money(inputs.get("ask")), "the request"
    if not ask:
        ask, ask_from = field_money(fields, "AGENTS_BUYER_ASK_FIELDS", "Assignment Price,Buyer Price,Dispo Price")
    contract, contract_from = money(inputs.get("contract")), "the request"
    if not contract:
        contract, contract_from = field_money(fields, "AGENTS_DEAL_CONTRACT_FIELDS",
                                              "Contract Price,Purchase Price,Under Contract Price")
    a = match_args(task, subj, ask, contract)
    if a.detail_cap <= 0:
        out["why"] = ("today's SiftMap deed reads are used (AGENTS_DEAL_READS_PER_DAY) or the run cap is 0, "
                      "so no buyer could be read; ask again tomorrow")
        return out
    log_lines: list = []
    try:
        sm = CountingMap(siftmap())
    except MissingKey as exc:
        out["why"] = str(exc)
        return out
    try:
        quiet(log_lines, dbm.phase_sweep, a, work, client=sm)
        quiet(log_lines, dbm.phase_hydrate, a, work, client=sm)
        quiet(log_lines, dbm.phase_rank, a, work)
    except SystemExit as exc:
        count_reads(sm.details)
        (work / "log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        out["why"] = "the buyer search stopped: %s" % str(exc).replace("GATE FAILED: ", "")[:200]
        return out
    except Exception as exc:  # noqa: BLE001  (SiftMap refusing or down: said, not raised)
        count_reads(sm.details)
        out["why"] = "the buyer search failed: %s" % str(exc)[:200]
        return out
    count_reads(sm.details)
    budget = Budget(task.get("id") or 0, float(setting("AGENTS_DEAL_BUYERS_RUN_CAP_USD", 3.0)))
    quiet(log_lines, dbp.phase_crm, a, work, crm=crm())
    quiet(log_lines, dbp.phase_dossier, a, work)
    paid = paid_steps(a, work, budget, log_lines)
    quiet(log_lines, dbp.phase_export, a, work)
    (work / "log.txt").write_text("\n".join(log_lines), encoding="utf-8")
    for name in ("Buyers_%s.xlsx" % a.slug, "dial_sheet.csv", "ranked.csv"):
        if (work / name).is_file():
            shutil.copyfile(work / name, folder / name)
            out["files"].append(name)

    ranked = _load(work / "ranked.json", {})
    dossier = {d["rank"]: d for d in _load(work / "dossier.json", [])}
    top = sorted((r for r in ranked.get("ranked", []) if r.get("top")), key=lambda r: r["top"])
    buyers = []
    for r in top:
        d = dossier.get(r["top"]) or {}
        dm = (d.get("decision_makers") or [{}])[0]
        buyers.append({"rank": r["top"], "buyer": r["buyer"], "score": r["score"], "local": r["local_buys"],
                       "days": r["days_since_last"], "price_med": r.get("price_med"),
                       "dm": dm.get("name") or r.get("principal") or "", "numbers": len(d.get("phones") or [])})
    comps = ranked.get("comps") or {}
    unread = _load(work / "unread.json", {}).get("unread") or []
    out.update(ok=True, ask=ask, ask_from=ask_from if ask else "", contract=contract,
               contract_from=contract_from if contract else "", band=ranked.get("band") or [0, 0],
               buyers=buyers, ranked_n=len(ranked.get("ranked") or []), dropped=len(ranked.get("dropped") or []),
               observations=ranked.get("observations") or 0,
               retail={k: (comps.get("retail") or {}).get(k) for k in ("n", "median")},
               reads=sm.details, unread=len(unread), cap=a.detail_cap, searches=sm.searches,
               registry=(dbm.REG_DIR / "registry.json").is_file(), paid=paid,
               spent_usd=round(store.spent(task_id=int(task.get("id") or 0)), 2), skipped=budget.skipped)
    return out


def render(task: dict, r: dict) -> str:
    head = ":handshake: *Deal buyers: %s*  (%s)" % (r.get("street") or task.get("subject") or "this deal",
                                                   "live" if r.get("live") else "dry run")
    if not r.get("ok"):
        return safe_text(head + "\nNot run: %s" % (r.get("why") or "no reason given"))
    lines = [head]
    if r.get("ask"):
        lines.append("Ask %s (from %s) | buyers who paid %s to %s score on price" % (
            usd(r["ask"]), r.get("ask_from") or "the record", usd(r["band"][0]), usd(r["band"][1])))
    else:
        lines.append("No asking price on the request or the record, so price fit is not scored")
    rt = r.get("retail") or {}
    if rt.get("n"):
        lines.append("Retail comps nearby: %d, median %s" % (rt["n"], usd(rt.get("median") or 0)))
    if not r.get("buyers"):
        lines.append("No buyer with 3 or more purchases bought near here (%d ranked, %d dropped)" % (
            r.get("ranked_n") or 0, r.get("dropped") or 0))
    for b in (r.get("buyers") or [])[:10]:
        lines.append("%d. %s: score %s, %d buys nearby, last buy %s days ago%s%s" % (
            b["rank"], b["buyer"], b["score"], b["local"], b["days"],
            (", ask for %s" % b["dm"]) if b.get("dm") else "",
            (", %d number(s) on file" % b["numbers"]) if b.get("numbers") else ", no number on file"))
    reads = "Deed reads %d" % (r.get("reads") or 0)
    if r.get("unread"):
        reads += ", %d nearby houses left unread at the cap of %s" % (r["unread"], r.get("cap"))
    if not r.get("registry"):
        reads += " | no dispo registry on this box, so lifetime counts and principals are thin"
    lines.append(reads)
    paid = r.get("paid") or {}
    for s in (paid.get("ran") or []) + (paid.get("not_run") or [])[:2]:
        lines.append(":warning: " + s if s in (paid.get("not_run") or []) else s)
    for f in r.get("files") or []:
        label = {"dial_sheet.csv": "Dial sheet", "ranked.csv": "Every buyer ranked"}.get(f, "Buyers workbook")
        lines.append(file_line(task, label, f))
    lines.append("Nothing written to DataSift; nobody contacted%s." % (
        ", spent $%.2f" % r["spent_usd"] if r.get("spent_usd") else ""))
    return safe_text("\n".join(lines))
