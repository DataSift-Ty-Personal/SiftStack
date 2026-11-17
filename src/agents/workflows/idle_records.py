"""idle_records: the records on one DataSift list worth working again, ranked, with the owners who
hold several of them.

Atlas's workflow (team/roster.yaml). A list title in (Obituary, Tired Landlord, D4D Top 3000); out
comes a workbook: the ranked list, the bench, the owners holding two or more of the list's records,
and every record the gates dropped with its reason. A scheduled run ranks the lists named in
IDLE_RECORDS_LISTS (default: Obituary).

Two engines, picked by the list, so each list is ranked by the rules built for it:
  * the Obituary list   obituary_opportunity: the death-date gates (probate needs months to open),
                        six components weighted off that list's own measured spread.
  * any other list      the Dream 100 score (dream100_build.rank_records): deal size, condition,
                        investor, evidence, reach. Condition is the D4D street-view score, which only
                        the D4D list has; every other list scores 0 there and the sheet says so,
                        because a column of zeros dressed as a finding is how a ranking misleads.

Why it reads the way it does: both engines used to pull through workstation clients (the Deal Room
token store, a password login). The brain has the Open API key and a shared request budget, so it
reads through the budgeted autopilot client, at most IDLE_RECORDS_MAX_READS records a run (default
300), caching each record on the volume and reading it again after IDLE_RECORDS_REFRESH_DAYS (default
7). Records past the cap are named as not read yet, and the next run reads them.

Read-only and free: it writes nothing to the account, in dry mode or live.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .. import config, slack, store

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import obituary_opportunity as oo  # noqa: E402

CRM = None          # the selftest injects a fake; otherwise the budgeted Open API key client
NAME = "idle_records"
TODAY = None        # the selftest pins the date


def _setting(name: str, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return type(default)(float(raw)) if isinstance(default, int) else raw
    except ValueError:
        return default


def _crm():
    if CRM is not None:
        return CRM
    from ..autopilot.crm import CRM as Client  # noqa: PLC0415
    return Client()


def _today() -> date:
    return TODAY or date.today()


def _folder(task: dict) -> Path:
    d = config.RUNS_DIR / ("%s-%d" % (NAME, int(task["id"])))
    (d / "files").mkdir(parents=True, exist_ok=True)
    return d


def _cache_dir() -> Path:
    d = Path(os.environ.get("IDLE_RECORDS_DIR") or config.DATA_DIR / "idle_records")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return "_".join(p for p in "".join(c if c.isalnum() else " " for c in str(s)).split() if p) or "list"


def lists_to_rank(task: dict) -> list[str]:
    """The list a person asked for, else the scheduled default (IDLE_RECORDS_LISTS)."""
    asked = str((store.payload_of(task).get("inputs") or {}).get("list") or "").strip()
    if asked:
        return [asked]
    return [x.strip() for x in str(_setting("IDLE_RECORDS_LISTS", "Obituary")).split(",") if x.strip()]


def is_obituary_list(title: str) -> bool:
    return title.strip().casefold() == oo.OBIT_LIST_TITLE.casefold()


def _transport(crm):
    return lambda path, method="GET", body=None, headers=None: crm.call(path, method, body, headers=headers)


# --------------------------------------------------------------------------- the obituary engine

def _owner_groups(rows: list[dict], name_key: str, mail_key: str) -> list[dict]:
    """Owners holding two or more of the list's records, summed. An owner is grouped by name and
    mailing street together, since a common name alone joins strangers."""
    groups: dict = {}
    for r in rows:
        name = str(r.get(name_key) or "").strip()
        if not name:
            continue
        key = (name.casefold(), str(r.get(mail_key) or "").strip().casefold())
        groups.setdefault(key, []).append(r)
    return [g for g in groups.values() if len(g) >= 2]


def _append_portfolio_sheet(path: Path, groups: list[dict], title: str) -> None:
    """obituary_opportunity's workbook is its command line's output and stays exactly that; the
    team's copy gains one sheet after it is written."""
    from openpyxl import load_workbook  # noqa: PLC0415
    from openpyxl.styles import Font  # noqa: PLC0415
    wb = load_workbook(path)
    ws = wb.create_sheet("7 Portfolio owners")
    ws["A1"] = "Owners with two or more records on the %s list" % title
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = "Grouped by owner name and mailing street. One conversation can move several doors."
    head = ["Owner", "Mailing street", "Properties", "Combined value", "Best score", "Addresses"]
    for c, h in enumerate(head, 1):
        ws.cell(row=4, column=c, value=h).font = Font(bold=True)
    for i, g in enumerate(sorted(groups, key=lambda g: (-len(g), -sum(x.get("estimate_value") or 0 for x in g))), 5):
        ws.cell(row=i, column=1, value=g[0].get("owner_name"))
        ws.cell(row=i, column=2, value=g[0].get("mail_street"))
        ws.cell(row=i, column=3, value=len(g))
        ws.cell(row=i, column=4, value=sum(x.get("estimate_value") or 0 for x in g)).number_format = "$#,##0"
        ws.cell(row=i, column=5, value=max((x.get("score") for x in g if x.get("score") is not None), default=None))
        ws.cell(row=i, column=6, value="; ".join(str(x.get("street") or "") for x in g))
    for col, wdt in zip("ABCDEF", (30, 30, 10, 14, 10, 80)):
        ws.column_dimensions[col].width = wdt
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    tmp.replace(path)


def rank_obituary(crm, title: str, files: Path, cap: int, refresh: int) -> dict:
    cache = _cache_dir() / ("%s_raw.json" % _slug(title).lower())
    reader = oo.Reader(call=_transport(crm))
    oo.pull("", cache, reader=reader, list_title=title, max_reads=cap, refresh_days=refresh, today=_today())
    reads = int((json.loads(cache.read_text(encoding="utf-8")).get("last_run") or {}).get("reads") or 0)
    qualified, excluded, meta = oo.assemble(cache, oo.MIN_OBIT_MONTHS, _today())
    stamp = _today().strftime("%Y%m%d")
    name = "Idle_%s_%s.xlsx" % (_slug(title), stamp)
    oo.build_workbook(qualified, excluded, meta, files / name, oo.MIN_OBIT_MONTHS, _today(), 0.75, 6, 50)
    flat = qualified + [x for x in excluded if x.get("owner_name")]
    groups = _owner_groups(flat, "owner_name", "mail_street")
    _append_portfolio_sheet(files / name, groups, title)
    largest = max(groups, key=len) if groups else []
    return {"list": title, "engine": "obituary", "files": [name], "records": meta.get("total", 0), "reads": reads,
            "ranked": len(qualified), "excluded": len(excluded) - meta.get("pending", 0),
            "not_read_yet": meta.get("pending", 0), "failed_reads": meta.get("unhydrated", 0),
            "portfolio_owners": len(groups), "largest_portfolio": len(largest),
            "top": [(r.get("street") or "", r.get("score")) for r in qualified[:3]]}


# --------------------------------------------------------------------------- the Dream 100 engine

def _list_uuids(crm, list_uuid: str) -> tuple[list[str], bool]:
    """Every record uuid on the list (clean and incomplete), and whether the search window cut it."""
    out, cut = [], False
    for ptype in ("clean", "incomplete"):
        must = {"any_lists": [list_uuid], "property_type": ptype}
        rows = crm.rows(must)
        cut = cut or len(rows) >= 10000
        out += [r.get("uuid") for r in rows if r.get("uuid")]
    return list(dict.fromkeys(out)), cut


def _d4d_cohort(title: str) -> dict:
    """The D4D street-view rank and score, when the list IS the D4D list and its cohort file was put
    on the volume (IDLE_RECORDS_D4D_COHORT). Any other list has none."""
    import d4d_call_list as d4d  # noqa: PLC0415
    if title.strip().casefold() != d4d.DEFAULT_LIST.casefold():
        return {}
    path = Path(os.environ.get("IDLE_RECORDS_D4D_COHORT") or _cache_dir() / "d4d_cohort.json")
    if not path.is_file():
        return {}
    cohort: dict = {}
    for r in json.loads(path.read_text(encoding="utf-8")):
        u = r.get("saved_uuid")
        if u and (u not in cohort or (r.get("rank") or 9999) < (cohort[u].get("rank") or 9999)):
            cohort[u] = r
    return cohort


def list_words(title: str, n_top: int, weights: dict, has_condition: bool) -> dict:
    """The workbook's words for a list that is not the D4D cohort: nothing on the sheet may claim
    a source the list does not have."""
    import dream100 as d100  # noqa: PLC0415
    stated = d100._stated(d100.WEIGHT_SETS[d100.DEFAULT_WEIGHTS])["subtitle"]
    rest = 100 - int(weights.get("cond", 0))
    words = {
        "top_label": "Top %d" % n_top,
        "title": "Top %d on the %s list: the largest opportunities" % (n_top, title),
        "subtitle": "Ranked by opportunity score (%s). Built %s. Read-only snapshot of this account." % (
            stated, _today().isoformat()),
        "overview": "One row per property on the %s list. Joined on the CRM record: the record's DataFlik scores "
                    "and deal fields, and the lists attached in the account." % title,
        "reach": "Best dial tier on the owner's phones, read from the Dial tags on the record: Dial First 10, "
                 "Second 7, Third 4, Fourth 2. DNC or opt-out scores 0 and is marked mail only.",
        "portfolio_sheet": "Owners holding two or more properties on the %s list, summed. One conversation can move "
                           "several doors." % title,
        "portfolio_title": "Owners with two or more properties on the %s list" % title,
        "phones_caveat": "Tiers are the Dial tags already on each phone; an untagged number counts as no tier. "
                         "Numbers disposed WRONG, DEAD or DNC were skipped.",
        "cohort_list": title,
    }
    if not has_condition:
        words.update({
            "overview": words["overview"] + " This list has no street-view condition score, so condition scores 0 "
                                            "for every row and the ranking rests on the other %d points." % rest,
            "condition": "No street-view condition score exists for this list, so condition scores 0 for every row.",
            "condition_caveat": "No street-view condition score for this list: drive the top rows before an offer.",
        })
    return words


def rank_list(crm, title: str, files: Path, cap: int, refresh: int) -> dict:
    import dream100 as d100  # noqa: PLC0415
    import dream100_build as build  # noqa: PLC0415
    lu = crm.list_uuid(title)
    if not lu:
        return {"list": title, "engine": "dream100", "problem": "no list titled %r in this account" % title}
    uuids, cut = _list_uuids(crm, lu)
    cache_path = _cache_dir() / _slug(title).lower() / "full_records.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    on_list = set(uuids)
    full = {u: r for u, r in cached.items() if u in on_list}      # a record that left the list leaves the ranking

    def age(u: str) -> str:
        return str((full.get(u) or {}).get("_fetched_at") or "")

    due = [u for u in uuids if u not in full
           or (_today() - (date.fromisoformat(age(u)[:10]) if age(u) else date.min)).days > refresh]
    due.sort(key=lambda u: (u in full, age(u)))
    reads = 0
    for u in due[:cap]:
        rec = crm.record(u)
        reads += 1
        if rec:
            rec["_fetched_at"] = _today().isoformat()
            full[u] = rec
    cache_path.write_text(json.dumps(full, default=str), encoding="utf-8")
    not_read = [u for u in uuids if u not in full]

    cohort = _d4d_cohort(title)
    chosen = d100.weights_name(None)
    w = d100.apply_weights(chosen, d100._build_module(build))
    try:
        ranked, excluded, portfolios = build.rank_records(full, cohort, {})
    finally:
        d100.apply_weights(d100.DEFAULT_WEIGHTS, build)
    n_top = int(_setting("IDLE_RECORDS_TOP", 100))
    top, bench = ranked[:n_top], ranked[n_top:n_top + 200]
    stamp = _today().strftime("%Y%m%d")
    book = "Idle_%s_%s.xlsx" % (_slug(title), stamp)
    words = None if cohort else list_words(title, n_top, w, has_condition=False)
    build.write_workbook(top, bench, ranked, excluded, portfolios, files / book, n_top, words=words)
    if chosen != d100.DEFAULT_WEIGHTS:
        d100.restate_workbook(files / book, chosen)
    twin = "Idle_%s_%s.csv" % (_slug(title), stamp)
    with (files / twin).open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(build.csv_rows(top))
    largest = max((p["n"] for p in portfolios), default=0)
    return {"list": title, "engine": "dream100", "files": [book, twin], "records": len(uuids), "ranked": len(ranked),
            "excluded": len(excluded), "not_read_yet": len(not_read), "reads": reads, "window_cut": cut,
            "weights": chosen, "condition": bool(cohort), "portfolio_owners": len(portfolios),
            "largest_portfolio": largest, "top": [(r.get("street") or "", r.get("opp")) for r in ranked[:3]]}


# --------------------------------------------------------------------------- the workflow

def run(task: dict) -> dict:
    folder = _folder(task)
    files = folder / "files"
    cap = int(_setting("IDLE_RECORDS_MAX_READS", 300))
    refresh = int(_setting("IDLE_RECORDS_REFRESH_DAYS", 7))
    crm = _crm()
    done = []
    for title in lists_to_rank(task):
        left = max(cap - sum(int(x.get("reads") or 0) for x in done), 0)
        try:
            if is_obituary_list(title):
                done.append(rank_obituary(crm, title, files, left, refresh))
            else:
                done.append(rank_list(crm, title, files, left, refresh))
        except RuntimeError as exc:          # an unknown list from the obituary reader, an API refusal
            done.append({"list": title, "problem": str(exc)[:300]})
    return {"run_dir": str(folder), "lists": done, "cap": cap, "at": datetime.now().isoformat(timespec="seconds")}


def render(task: dict, result: dict) -> str:
    lines = []
    for x in result.get("lists") or []:
        if x.get("problem"):
            lines.append("Idle records on the %s list did not run: %s" % (x["list"], x["problem"]))
            continue
        head = "Idle records on the %s list: %d ranked, %d gated out" % (x["list"], x["ranked"], x["excluded"])
        if x.get("not_read_yet"):
            head += ", %d not read yet (the next run reads them)" % x["not_read_yet"]
        lines.append(head + ".")
        if x.get("top"):
            lines.append("Top: " + "; ".join("%s (%s)" % (s, sc) for s, sc in x["top"]) + ".")
        if x.get("portfolio_owners"):
            lines.append("%d owners hold two or more of these records; the largest holds %d." % (
                x["portfolio_owners"], x["largest_portfolio"]))
        if x.get("engine") == "dream100" and not x.get("condition"):
            lines.append("This list has no street-view condition score, so the ranking rests on the other factors.")
        if x.get("window_cut"):
            lines.append("The list holds more than the search reads in one pass (10,000 rows); the rest were not ranked.")
        for n in x.get("files") or []:
            u = slack.file_url(task["id"], n)
            lines.append("%s: %s" % (n, u) if u else n)
    return "\n".join(lines)
