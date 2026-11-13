"""Deep prospect a whole list: count it, price it, run it.

The Research agent's deep prospect (src/agents/research/deep_prospect.py) works one record at a
time, fired by a status change. This runs the same chain over a backlog: every workable record on
the Foreclosure, Probate and Obituary lists, a few at a time, resumable, with a spend ceiling.
It defaults to the low-cost research engine (two inexpensive models, cross-checked; see
src/agents/research/open_research.py).

    python src/deep_prospect_batch.py cohort                          # read-only: who is in scope, by list
    python src/deep_prospect_batch.py estimate                        # cost per record and per list, from finished runs
    python src/deep_prospect_batch.py run --list Foreclosure --limit 7            # dry: research only, no vendors, no CRM writes
    python src/deep_prospect_batch.py run --list Foreclosure --limit 7 --commit   # live: skip trace, score, write back
    python src/deep_prospect_batch.py run --addresses "123 Example St;45 Sample Ave" --commit
    python src/deep_prospect_batch.py status

Scope rules (cohort): a record is in scope when it sits on the list, is not tagged `recently sold`,
`Deep Prospected` or `Do Not Market`, is not in a dead status, has a street, and is owned by a
person (an LLC or trust needs the entity path, not a name trace). Foreclosure counts only the
courthouse records. A record on two lists is researched once, under the first list in the order
Foreclosure, Probate, Obituary.

Then every survivor's FULL record is read (the search rows carry no notes, sale or listing fields)
and three more gates apply, each counted: the newest courthouse auction date in the notes must
still be ahead (today counts as passed; a courthouse foreclosure with no date is held out), the
last sale on the record must predate the notice or the death, and the house must not be listed
or pending on the MLS. The same check runs again right before each record in `run`, because
a cohort is a snapshot and an auction passes while a batch waits. `--force` skips it.

Nothing here posts to Slack: a backlog run that posted once per record would bury the channel.
The run prints one line per record and a two-line total.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

OUT = Path(os.environ.get("SIFTSTACK_OUTPUT_DIR") or ROOT / "output") / "deep_prospect_batch"
COHORT = OUT / "cohort.json"
LOG = OUT / "batch_log.jsonl"
LISTS = ("Foreclosure", "Probate", "Obituary")
COURTHOUSE_ONLY = {"Foreclosure"}                 # its bulk rows are not first-to-market
EXCLUDE_TAGS = ("recently sold", "Deep Prospected", "Do Not Market")
DEAD_STATUSES = {"sold", "not_interested", "not interested", "do_not_contact", "dnc", "dead", "wrong_number"}
ENTITY_RX = re.compile(r"\b(llc|l\.l\.c|inc|corp|company|trust|trustee|lp|llp|properties|holdings|church|bank|"
                       r"association|partners|investments|group|ministries|foundation)\b", re.I)
AUCTION_RX = re.compile(r"Auction:\s*(\d{1,2})/(\d{1,2})/(20\d\d)")     # the formatter's own note line
MONTH_RX = re.compile(r"^(20\d\d)-(\d\d)$")                             # the notice month tag
NOTE_DAY_RX = re.compile(r"^(20\d\d-\d\d-\d\d)\s*$", re.M)
MLS_LISTED = ("listed", "active", "pending", "contingent", "under contract", "coming soon")   # the record's `mls`;
#                                                   seen live: Off Market, blank, Pending, Sold, Listed
SOLD_LOOKBACK_DAYS = 365                          # the sold test when a record carries no notice or death date


# ------------------------------------------------------- the record's own facts --

def _names(items) -> list[str]:
    out = []
    for t in items or []:
        n = (t.get("name") or t.get("title")) if isinstance(t, dict) else t
        if n:
            out.append(str(n))
    return out


def _day(value) -> str:
    return str(value or "")[:10] if value else ""


def facts_of(rec: dict) -> dict:
    """The few fields of a FULL record that decide whether it is still worth working."""
    notes, tags = rec.get("notes") or "", _names(rec.get("tags"))
    return {"auctions": sorted({"%s-%02d-%02d" % (y, int(m), int(d)) for m, d, y in AUCTION_RX.findall(notes)}),
            "months": sorted(t for t in tags if MONTH_RX.match(t)),
            "note_days": sorted(set(NOTE_DAY_RX.findall(notes))),
            "last_sold": _day(rec.get("last_sold")), "mls": str(rec.get("mls") or "").strip(),
            "obituary": _day(rec.get("last_obituary_date")), "probate_open": _day(rec.get("probate_open_date")),
            "status": str(rec.get("status") or "").strip(), "tags": tags, "lists": _names(rec.get("lists"))}


def title_of(f: dict) -> str:
    """Which of the three lists a named record is worked under, in the cohort's own order."""
    on = {x.strip().lower() for x in f.get("lists") or []}
    if "foreclosure" in on and (f.get("auctions") or "courthouse data" in {t.lower() for t in f.get("tags") or []}):
        return "Foreclosure"
    return next((t for t in ("Probate", "Obituary") if t.lower() in on), "")


def ref_day(title: str, f: dict) -> str:
    """The day the distress began: a sale on or after it means the house has changed hands since."""
    first_month = f["months"][0] + "-01" if f.get("months") else ""
    note = (f.get("note_days") or [""])[0]
    if title == "Obituary":
        return f.get("obituary") or first_month or note
    if title == "Probate":
        return min([x for x in (f.get("probate_open"), first_month) if x] or [note])
    return first_month or note


def detail_gate(title: str, f: dict, today: date) -> str:
    """Why a record is no longer worth working, read off its own full record, or '' when it is."""
    day, auctions = today.isoformat(), f.get("auctions") or []
    if auctions and auctions[-1] <= day:                    # a sale today is over before a file is ready
        return "auction date passed"
    if title == "Foreclosure" and not auctions:
        return "no sale date on record"
    sold = f.get("last_sold") or ""
    ref = ref_day(title, f) or (today - timedelta(days=SOLD_LOOKBACK_DAYS)).isoformat()
    if sold and sold >= ref:
        return "sold since the notice or death"
    # An MLS status of Sold is NOT a gate: it mirrors the last sale, which is often the current
    # owner's own purchase (seen live: bought in January, noticed in November, sale date in January).
    if (f.get("mls") or "").strip().lower().startswith(MLS_LISTED):
        return "listed or pending on the MLS"
    return ""


def run_gate(rec: dict, title: str, today: date, named: bool = False, redo: bool = False) -> str:
    """The last check before money is spent on one record. A named record skips the tag and status
    gates (someone asked for it by name) but never the facts about the house itself."""
    f = facts_of(rec)
    why = detail_gate(title or title_of(f), f, today)
    if why or named:
        return why
    if f["status"].lower() in DEAD_STATUSES:
        return "dead status"
    have = {t.lower() for t in f["tags"]}
    for t in EXCLUDE_TAGS:
        if t.lower() in have and not (redo and t == "Deep Prospected"):
            return "tagged " + t
    return ""


def _facts_path() -> Path:
    return OUT / "record_facts.json"


def load_facts(today: date) -> dict:
    """Facts read earlier TODAY. An older read is never reused: an auction date passes overnight."""
    p = _facts_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        d = {}
    return {u: f for u, f in d.items() if f.get("read") == today.isoformat()}


def save_facts(facts: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _facts_path().write_text(json.dumps(facts), encoding="utf-8")


def read_facts(call, uuid: str, today: date) -> dict | None:
    try:
        f = facts_of(call("/api/internal/property/%s/" % uuid))
    except Exception:  # noqa: BLE001  (a record we cannot read is held out, never assumed workable)
        return None
    f["read"] = today.isoformat()
    return f


# ------------------------------------------------------------------ cohort --

def _crm(gap: float = 0.5):
    import datasift_api_upload as up
    api = up.Api()

    def call(path, method="GET", body=None, headers=None):
        for attempt in range(6):
            try:
                time.sleep(gap)
                return api.call(path, method, body, headers=headers)
            except up.ApiError as exc:
                if getattr(exc, "code", 0) not in (429, 502, 503, 504):
                    raise
                m = re.search(r"available in (\d+)", str(exc))
                time.sleep(int(m.group(1)) + 2 if m else 10 * (attempt + 1))
        raise RuntimeError("gave up on " + path)
    return call


def _titles(call, path: str) -> dict:
    r = call(path)
    rows = r.get("results", r) if isinstance(r, dict) else r
    return {(x.get("title") or "").strip(): x["uuid"] for x in rows}


def _search(call, must: dict, limit: int, offset: int) -> dict:
    return call("/api/internal/property/", "POST",
                {"limit": limit, "offset": offset, "ordering": "-created", "query": {"must": must}},
                headers={"x-http-method-override": "GET"})


def is_entity(owner: dict) -> bool:
    company = (owner.get("company") or "").strip()
    name = " ".join(p for p in (owner.get("first_name"), owner.get("last_name")) if p).strip()
    if company and not name:
        return True
    return bool(ENTITY_RX.search(name)) or not (owner.get("first_name") and owner.get("last_name"))


def gate(row: dict) -> str:
    """Why a search row is out of scope, or '' when it is in."""
    if (row.get("status") or "").strip().lower() in DEAD_STATUSES:
        return "dead status"
    if not ((row.get("address") or {}).get("street") or "").strip():
        return "no street"
    if is_entity(row.get("owner") or {}):
        return "entity or unnamed owner"
    return ""


def build_cohort(call=None, today: date | None = None, fresh: bool = False, quiet: bool = True) -> dict:
    call, today = call or _crm(), today or date.today()
    lists, tags = _titles(call, "/api/internal/list/?limit=1000"), _titles(call, "/api/internal/tag/?limit=10000")
    exclude = [tags[t] for t in EXCLUDE_TAGS if t in tags]
    out, taken = {"built": datetime.now().isoformat(timespec="seconds"), "lists": {}}, set()
    facts, barred, reads = ({} if fresh else load_facts(today)), {}, 0
    for title in LISTS:
        if title not in lists:
            out["lists"][title] = {"error": "no list named %r in this account" % title}
            continue
        base = {"property_type": "clean", "any_lists": [lists[title]]}
        if title in COURTHOUSE_ONLY and "Courthouse Data" in tags:
            base["any_tags"] = [tags["Courthouse Data"]]
        on_list = _search(call, base, 1, 0).get("count") or 0
        counts = Counter()
        for t in EXCLUDE_TAGS:                       # by subtraction: `must_not` is the proven grammar
            if t in tags:
                without = _search(call, dict(base, must_not={"any_tags": [tags[t]]}), 1, 0).get("count") or 0
                counts["tagged " + t] = on_list - without
        must = dict(base, must_not={"any_tags": exclude}) if exclude else base
        rows, offset, after_tags = [], 0, 0
        while True:
            r = _search(call, must, 100, offset)
            after_tags = r.get("count") or 0
            page = r.get("results") or []
            for x in page:
                why, f = gate(x), None
                if not why and x["uuid"] in taken:
                    why = "already under another list"
                if not why and x["uuid"] in barred:
                    why = "held out under an earlier list"
                if not why:
                    f = facts.get(x["uuid"])
                    if f is None:
                        f = read_facts(call, x["uuid"], today)
                        reads += 1
                        if f is not None:
                            facts[x["uuid"]] = f
                        if reads % 100 == 0:
                            save_facts(facts)
                            if not quiet:
                                print("  %s: %d full records read" % (title, reads), flush=True)
                    why = "could not read the record" if f is None else detail_gate(title, f, today)
                    if why and f is not None:
                        barred[x["uuid"]] = why
                if why:
                    counts[why] += 1
                    continue
                taken.add(x["uuid"])
                a, o = x.get("address") or {}, x.get("owner") or {}
                rows.append({"uuid": x["uuid"], "street": a.get("street"), "city": a.get("city"),
                             "zip": str(a.get("postal_code") or a.get("zip5") or "")[:5], "status": x.get("status") or "",
                             "owner": " ".join(p for p in (o.get("first_name"), o.get("last_name")) if p),
                             "sale_date": (f.get("auctions") or [""])[-1]})
            offset += 100
            if not page or offset >= after_tags or offset >= 9900:
                break
        if title == "Foreclosure":
            rows.sort(key=lambda r: r["sale_date"] or "9999")     # the soonest sale is worked first
        out["lists"][title] = {"on_list": on_list, "after_tag_gates": after_tags, "in_scope": len(rows),
                               "gated": dict(counts), "rows": rows}
    save_facts(facts)
    OUT.mkdir(parents=True, exist_ok=True)
    COHORT.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


def print_cohort(c: dict) -> None:
    total = 0
    for title, d in c["lists"].items():
        if d.get("error"):
            print("%-12s %s" % (title, d["error"]))
            continue
        total += d["in_scope"]
        print("%-12s on list %5d | in scope %5d | out: %s" % (
            title + (" (courthouse)" if title in COURTHOUSE_ONLY else ""), d["on_list"], d["in_scope"],
            ", ".join("%s %d" % kv for kv in sorted(d["gated"].items())) or "none"))
    print("In scope across the three lists: %d records (each counted once). Saved %s" % (total, COHORT))


# --------------------------------------------------------------------- run --

def _log(entry: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def read_log() -> list[dict]:
    if not LOG.exists():
        return []
    return [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines() if line.strip()]


def done_uuids(live: bool) -> set[str]:
    """Records a batch already finished in this mode. A dry run never blocks the later live one."""
    return {e["uuid"] for e in read_log() if e.get("ok") and bool(e.get("live")) == live}


def one(uuid: str) -> int:
    """Child process: one record through the agent's deep prospect, with its spend on the books."""
    from agents import store
    from agents.research import deep_prospect
    store.init()
    task_id, _ = store.enqueue("research", "deep_prospect", uuid, subject="batch", force=True)
    store.mark(task_id, "running")               # never left queued for a worker to pick up twice
    t0, entry = time.time(), {"uuid": uuid, "task_id": task_id, "at": datetime.now().isoformat(timespec="seconds")}
    try:
        r = deep_prospect.run({"id": task_id, "record_uuid": uuid, "agent": "research", "workflow": "deep_prospect"})
        store.finish(task_id, "done", result=r)
        entry.update(ok=True, live=r.get("live"), slug=r.get("slug"), street=r.get("street"),
                     owner_status=r.get("owner_status"), decision_maker=r.get("decision_maker"),
                     written_back=r.get("written_back"), spent_usd=r.get("spent_usd"),
                     skipped=r.get("skipped"), pdf=r.get("pdf"), run_dir=r.get("run_dir"))
    except BaseException as exc:  # noqa: BLE001  (the chain's gates raise SystemExit)
        if isinstance(exc, KeyboardInterrupt):
            raise
        store.finish(task_id, "failed", error="%s: %s" % (type(exc).__name__, str(exc)[:500]))
        entry.update(ok=False, error="%s: %s" % (type(exc).__name__, str(exc)[:300]))
    entry["seconds"] = round(time.time() - t0)
    entry["phases"] = phase_spend(task_id)
    _log(entry)
    print("RESULT " + json.dumps(entry, default=str), flush=True)
    return 0 if entry.get("ok") else 1


def phase_spend(task_id: int) -> dict:
    from agents import store
    with store._db() as c:
        rows = c.execute("SELECT what, SUM(usd) AS usd FROM spend WHERE task_id=? GROUP BY what", (task_id,)).fetchall()
    return {r["what"]: round(r["usd"], 4) for r in rows}


def pick(args) -> list[dict]:
    if args.uuids:
        return [{"uuid": u.strip(), "street": ""} for u in args.uuids.split(",") if u.strip()]
    if args.addresses:
        import crm_key_client
        crm, out = crm_key_client.from_env(), []
        for addr in [a.strip() for a in args.addresses.split(";") if a.strip()]:
            hits = crm.search_by_address(addr, limit=5)
            if len(hits) != 1:
                print("  %r matches %d records; skipped (pass the full street line)" % (addr, len(hits)))
                continue
            out.append({"uuid": hits[0]["uuid"], "street": addr})
        return out
    if not COHORT.exists():
        raise SystemExit("no cohort yet: run `python src/deep_prospect_batch.py cohort` first")
    d = json.loads(COHORT.read_text(encoding="utf-8"))["lists"].get(args.list or "") or {}
    if not d.get("rows"):
        raise SystemExit("nothing in scope for --list %r" % args.list)
    return d["rows"]


def run_batch(args) -> int:
    live = bool(args.commit)
    rows = [r for r in pick(args) if args.redo or r["uuid"] not in done_uuids(live)]
    rows = rows[:args.limit] if args.limit else rows
    env = dict(os.environ, AGENTS_MODE="live" if live else "dry", AGENTS_RESEARCH_ENGINE=args.engine,
               PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    if args.analysis_model:
        env["AGENTS_ANALYSIS_MODEL"] = args.analysis_model
    if args.daily_cap:
        env["AGENTS_DAILY_CAP_USD"] = str(args.daily_cap)
    py = str(ROOT / ".venv" / "Scripts" / "python.exe")
    py = py if Path(py).exists() else sys.executable
    print("%d record(s), %s, %s research engine, %d at a time, stop at $%.2f"
          % (len(rows), "LIVE (vendors + CRM write-back)" if live else "dry (research only)", args.engine,
             args.workers, args.max_usd), flush=True)
    spent, results = 0.0, []
    call, lock, named = (None if args.force else _crm()), threading.Lock(), not args.list

    def held(row) -> str:
        """Re-read the record right before paying for it: the cohort is a snapshot."""
        if call is None:
            return ""
        try:
            with lock:
                rec = call("/api/internal/property/%s/" % row["uuid"])
        except Exception as exc:  # noqa: BLE001
            return "could not read the record (%s)" % str(exc)[:60]
        return run_gate(rec, args.list or "", date.today(), named=named, redo=args.redo)

    def launch(row):
        nonlocal spent
        if spent >= args.max_usd:
            return {"uuid": row["uuid"], "ok": False, "error": "not started: the batch ceiling was reached"}
        why = held(row)
        if why:
            print("  %-28s HELD OUT  %s" % ((row.get("street") or row["uuid"][:8])[:28], why), flush=True)
            return {"uuid": row["uuid"], "ok": False, "held": why}
        p = subprocess.run([py, str(Path(__file__)), "one", "--uuid", row["uuid"]], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        line = next((ln for ln in reversed((p.stdout or "").splitlines()) if ln.startswith("RESULT ")), "")
        e = json.loads(line[7:]) if line else {"uuid": row["uuid"], "ok": False,
                                              "error": "no result (rc %s) %s" % (p.returncode, (p.stderr or "")[-300:])}
        spent += float(e.get("spent_usd") or 0)
        print("  %-28s %-9s $%5.2f %4ss  %s" % (
            (e.get("street") or row.get("street") or row["uuid"][:8])[:28],
            (e.get("owner_status") or ("FAILED" if not e.get("ok") else "-")), float(e.get("spent_usd") or 0),
            e.get("seconds", "-"), (e.get("decision_maker") or e.get("error") or "")[:70]), flush=True)
        return e

    with ThreadPoolExecutor(max(1, args.workers)) as ex:
        results = list(ex.map(launch, rows))
    ok = [e for e in results if e.get("ok")]
    out = [e for e in results if e.get("held")]
    print("Done: %d of %d finished, %d written back, $%.2f spent ($%.2f a record)."
          % (len(ok), len(results), sum(1 for e in ok if e.get("written_back")), spent, spent / max(1, len(ok))))
    if out:
        print("Held out, nothing spent: %d (%s)" % (len(out), ", ".join(
            "%s %d" % kv for kv in Counter(e["held"].split(" (")[0] for e in out).most_common())))
    failed = [e for e in results if not e.get("ok") and not e.get("held")]
    if failed:
        print("Failed: " + "; ".join("%s (%s)" % (e["uuid"][:8], (e.get("error") or "")[:80]) for e in failed[:6]))
    return 0 if (ok or out) and not failed else 1


# ---------------------------------------------------------------- estimate --

def measured(entries: list[dict]) -> dict:
    """Average cost of each phase over finished live runs."""
    live = [e for e in entries if e.get("ok") and e.get("live")]
    phases = Counter()
    for e in live:
        for what, usd in (e.get("phases") or {}).items():
            phases[re.sub(r"\s*\(.*\)$", "", what)] += usd
    n = max(1, len(live))
    return {"records": len(live), "per_record": round(sum(float(e.get("spent_usd") or 0) for e in live) / n, 3),
            "by_phase": {k: round(v / n, 3) for k, v in sorted(phases.items(), key=lambda kv: -kv[1])},
            "seconds": round(sum(e.get("seconds") or 0 for e in live) / n)}


def estimate() -> int:
    m = measured(read_log())
    if not m["records"]:
        print("No finished live runs yet, so there is nothing measured to price from.")
        return 1
    print("Measured on %d live record(s): $%.2f a record, about %d min each" % (m["records"], m["per_record"], m["seconds"] // 60))
    for k, v in m["by_phase"].items():
        print("   %-34s $%.3f" % (k, v))
    if COHORT.exists():
        total = 0
        for title, d in json.loads(COHORT.read_text(encoding="utf-8"))["lists"].items():
            n = d.get("in_scope") or 0
            total += n
            print("%-12s %5d records  about $%8.2f" % (title, n, n * m["per_record"]))
        print("%-12s %5d records  about $%8.2f" % ("All three", total, total * m["per_record"]))
    return 0


def status() -> int:
    es = read_log()
    ok = [e for e in es if e.get("ok")]
    print("%d runs logged: %d finished (%d live, %d written back), %d failed, $%.2f spent"
          % (len(es), len(ok), sum(1 for e in ok if e.get("live")), sum(1 for e in ok if e.get("written_back")),
             len(es) - len(ok), sum(float(e.get("spent_usd") or 0) for e in es)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cohort")
    c.add_argument("--fresh", action="store_true", help="re-read every record, ignoring facts read earlier today")
    sub.add_parser("estimate")
    sub.add_parser("status")
    o = sub.add_parser("one")
    o.add_argument("--uuid", required=True)
    r = sub.add_parser("run")
    r.add_argument("--list", choices=LISTS)
    r.add_argument("--uuids", default="")
    r.add_argument("--addresses", default="", help="street lines separated by ;")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--commit", action="store_true", help="live: pay vendors and write back to the CRM")
    r.add_argument("--engine", default="open", choices=("open", "claude"))
    r.add_argument("--analysis-model", default="")
    r.add_argument("--max-usd", type=float, default=25.0, help="stop starting records once the batch has spent this")
    r.add_argument("--daily-cap", type=float, default=0.0, help="override AGENTS_DAILY_CAP_USD for this batch")
    r.add_argument("--redo", action="store_true", help="run records a batch already finished")
    r.add_argument("--force", action="store_true",
                   help="skip the last check (auction passed, sold, listed) before each record")
    a = ap.parse_args(argv)
    if a.cmd == "cohort":
        print_cohort(build_cohort(fresh=a.fresh, quiet=False))
        return 0
    if a.cmd == "estimate":
        return estimate()
    if a.cmd == "status":
        return status()
    if a.cmd == "one":
        return one(a.uuid)
    if not (a.list or a.uuids or a.addresses):
        ap.error("run needs --list, --uuids or --addresses")
    return run_batch(a)


if __name__ == "__main__":
    sys.exit(main())
