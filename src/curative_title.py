"""Curative title, end to end: rank the houses whose owner died and whose title is stuck, research every heir,
check whose phone each number is, and hand the work-now houses to the Curative Title board.

    python src/curative_title.py rank --county "Knox, TN"           # read your records, rank them, write the workbook
    python src/curative_title.py family  --market knox-tn [--quote | --commit]    # SmartSkip family trees (paid)
    python src/curative_title.py stories select   --market knox-tn --top 25       # pick the houses to research
    python src/curative_title.py stories prepare  --market knox-tn                # one input.json per house
    python src/curative_title.py stories run      --market knox-tn                # each house in its own Claude Code session
    python src/curative_title.py stories status   --market knox-tn
    python src/curative_title.py stories summarize --market knox-tn
    python src/curative_title.py phones  --market knox-tn [--commit]             # Trestle: whose number is whose (paid)
    python src/curative_title.py score   --market knox-tn                         # re-rank with the stories, new workbook
    python src/curative_title.py next    --market knox-tn                         # where this market stands, what to run next
    python src/curative_title.py widen   --county "Knox, TN" [--commit --approve N]   # pull more death-flagged houses in

Then load the work-now houses onto the board (the loader writes to DataSift; dry unless --commit and CURATIVE_ALLOW=1):
    python src/agents/autopilot/curative_setup.py board
    python src/agents/autopilot/curative_setup.py load --results output/curative_title/knox-tn/stories

Everything reads your own account over the DataSift Open API key (REISIFT_API_KEY in .env) and free public county
sources; paid vendors (SmartSkip, Enformion, Trestle) are used only with your own keys and only with --commit.
Full guide: docs/curative-title.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from curative_engine import common as C          # noqa: E402
from curative_engine.counties import resolve     # noqa: E402


def _csv(v: str) -> list[str]:
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def workspace(args) -> C.Workspace:
    if getattr(args, "dir", ""):
        root = Path(args.dir)
        return C.Workspace(root.name, base=root.parent)
    market = getattr(args, "market", "") or ""
    if not market:
        counties = getattr(args, "county", None) or []
        if not counties:
            raise SystemExit("name the market (--market) or the county (--county \"Knox, TN\")")
        c = resolve(counties[0])
        market = "%s-%s" % (c.name, c.state)
    return C.Workspace(market)


def say_next(ws: C.Workspace, out=print) -> None:
    """Where the market stands and the next command to run."""
    scored = ws.read("scored.json") or []
    q = C.read_json(ws.stories / "queue.json") or []
    fam = ws.read("family.json") or {}
    from curative_engine import stories as S
    _, best = S.collect(ws) if ws.houses.exists() else ([], [])
    work = [r for r in best if str(r.get("verdict")).startswith("Work now")]
    checked = any((ws.houses / r["slug"] / "phones_checked.json").exists() for r in best)
    m = ws.market
    lines = []
    if not scored:
        lines.append('rank the market:            python src/curative_title.py rank --market %s --county "<County, ST>"' % m)
    elif not fam:
        lines.append("family trees (optional, paid): python src/curative_title.py family --market %s --quote" % m)
    if scored and not q:
        lines.append("pick houses to research:    python src/curative_title.py stories select --market %s --top 25" % m)
    elif q and not best:
        lines.append("research the heirs:         python src/curative_title.py stories prepare --market %s, then stories run" % m)
    if best and not checked:
        lines.append("check the heirs' numbers:   python src/curative_title.py phones --market %s (then --commit)" % m)
    if work:
        lines.append("load the work-now houses:   python src/agents/autopilot/curative_setup.py load --results %s" % ws.stories)
    out("%s: %d house(s) ranked, %d researched (%d work now)." % (m, len(scored), len(best), len(work)))
    for ln in lines:
        out("  next: " + ln)


def cmd_rank(args) -> int:
    from curative_engine import account, assemble, gather, score, workbook
    ws = workspace(args)
    s = ws.settings()
    counties = args.county or s.get("counties") or []
    lists = _csv(args.lists) or s.get("lists") or list(account.DEFAULT_LISTS)
    tags = _csv(args.tags) if args.tags is not None else (s.get("tags") or [])
    resolved = [resolve(c) for c in counties]
    ws.save_settings({"counties": counties, "lists": lists, "tags": tags})
    crm = account.crm_client()
    sm = None if args.no_siftmap else account.siftmap_client()
    account.pull(ws, crm, sm, lists=lists, tags=tags, counties=[c.name for c in resolved],
                 states=[c.state for c in resolved if c.state], fresh_days=args.fresh_days, max_records=args.max_records)
    if not args.no_county:
        gather.county(ws, workers=args.workers)
    rows = assemble.run(ws)
    if not args.no_obits:
        gather.obituaries(ws, rows)
    score.run(ws)
    workbook.build(ws)
    say_next(ws)
    return 0


def cmd_score(args) -> int:
    from curative_engine import assemble, score, workbook
    ws = workspace(args)
    if args.reassemble:
        assemble.run(ws)
    score.run(ws, refresh_probate=args.refresh_probate)
    workbook.build(ws)
    say_next(ws)
    return 0


def cmd_family(args) -> int:
    from curative_engine import family
    ws = workspace(args)
    family.order(ws, top=args.top, quote=args.quote, commit=args.commit, max_usd=args.max_usd)
    if args.commit:
        from curative_engine import score
        score.run(ws)
    return 0


def cmd_stories(args) -> int:
    from curative_engine import stories as S
    ws = workspace(args)
    if args.action == "select":
        S.select(ws, top=args.top, only=set(_csv(args.only)), redo=args.redo)
        S.prepare(ws)
    elif args.action == "prepare":
        S.prepare(ws)
    elif args.action == "run":
        queue = C.read_json(ws.stories / "queue.json") or []
        only = set(_csv(args.only))
        if only:
            queue = [q for q in queue if q["slug"] in only]
        if not queue:
            raise SystemExit("no houses queued: run `stories select` first")
        missing = [q["slug"] for q in queue if not (ws.houses / q["slug"] / "input.json").exists()]
        if missing:
            S.prepare(ws)
        r = S.Runner(ws, concurrency=args.concurrency, daily_usd=args.daily_usd, house_usd=args.house_usd,
                     model=args.model, auth=args.auth)
        r.run(queue, limit=args.limit, wait=not args.no_wait)
        S.summarize(ws)
    elif args.action == "status":
        S.status(ws)
    elif args.action == "summarize":
        S.summarize(ws)
    say_next(ws)
    return 0


def cmd_phones(args) -> int:
    from curative_engine import phones, stories as S
    ws = workspace(args)
    phones.check(ws, commit=args.commit, max_pairs=args.max_pairs, only=set(_csv(args.only)))
    if args.commit:
        S.summarize(ws)
    say_next(ws)
    return 0


def cmd_people(args) -> int:
    from curative_engine import stories as S
    ws = workspace(args)
    res = S.people_search(ws, args.house, args.first, args.last, args.middle, args.street, args.city, args.state,
                          args.zip, args.dob_year, args.max)
    print(json.dumps(res, indent=1, default=str))
    return 0 if not res.get("error") else 2


def cmd_widen(args) -> int:
    from curative_engine import account
    if not args.county:
        raise SystemExit('name the county: --county "Knox, TN"')
    ws = workspace(args)
    county = resolve(args.county[0])
    sm = account.siftmap_client()
    if sm is None:
        raise SystemExit("REISIFT_API_KEY is not set in .env")
    crm = account.crm_client() if args.commit else None
    account.widen(ws, sm, crm, county, approve=args.approve, commit=args.commit, max_ids=args.max)
    return 0


def cmd_workbook(args) -> int:
    from curative_engine import workbook
    workbook.build(workspace(args))
    return 0


def cmd_next(args) -> int:
    say_next(workspace(args))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Curative title: rank, research the heirs, check the numbers, load the board.",
                                 epilog="Guide: docs/curative-title.md")
    sub = ap.add_subparsers(dest="cmd")

    def common(p, county=False):
        p.add_argument("--market", default="", help="the market's folder name (default: the first county, e.g. knox-tn)")
        p.add_argument("--dir", default="", help=argparse.SUPPRESS)
        if county:
            p.add_argument("--county", action="append", default=None, help='a county, e.g. "Knox, TN" or 47093 (repeat for more)')

    r = sub.add_parser("rank", help="read your records, the county sources and SiftMap; score; write the workbook")
    common(r, county=True)
    r.add_argument("--lists", default="", help="comma list of list titles (default Obituary,Probate,Foreclosure)")
    r.add_argument("--tags", default=None, help="comma list of tag titles to add to the scope")
    r.add_argument("--fresh-days", type=int, default=7, help="a record read within this many days is not read again")
    r.add_argument("--max-records", type=int, default=0, help="read at most this many records this run (resumable)")
    r.add_argument("--workers", type=int, default=4, help="county roll reads at once")
    r.add_argument("--no-siftmap", action="store_true", help="skip SiftMap's detail")
    r.add_argument("--no-county", action="store_true", help="skip the county tax roll and city bills")
    r.add_argument("--no-obits", action="store_true", help="skip the obituary index lookups")
    r.set_defaults(fn=cmd_rank)

    s = sub.add_parser("score", help="score again (after family trees or heir stories) and write a new workbook")
    common(s)
    s.add_argument("--reassemble", action="store_true", help="join the saved reads again first")
    s.add_argument("--refresh-probate", action="store_true", help="download the probate index again")
    s.set_defaults(fn=cmd_score)

    f = sub.add_parser("family", help="SmartSkip family trees for the ranked houses (dry; --quote is free; --commit pays)")
    common(f)
    f.add_argument("--top", type=int, default=0, help="the top N houses not ruled out (default: tiers 1 and 2)")
    f.add_argument("--quote", action="store_true")
    f.add_argument("--commit", action="store_true")
    f.add_argument("--max-usd", type=float, default=60.0)
    f.set_defaults(fn=cmd_family)

    st = sub.add_parser("stories", help="the heir research")
    st.add_argument("action", choices=["select", "prepare", "run", "status", "summarize"])
    common(st)
    st.add_argument("--top", type=int, default=25)
    st.add_argument("--only", default="", help="comma list of house slugs")
    st.add_argument("--redo", action="store_true", help="select houses that already have a story too")
    st.add_argument("--concurrency", type=int, default=3)
    st.add_argument("--daily-usd", type=float, default=100.0, help="Claude Code usage a day (its own estimate)")
    st.add_argument("--house-usd", type=float, default=10.0)
    st.add_argument("--model", default="opus")
    st.add_argument("--auth", choices=["plan", "api"], default="plan",
                    help="plan: Claude Code's own sign-in (an API key is left out); api: pass ANTHROPIC_API_KEY")
    st.add_argument("--limit", type=int, default=0)
    st.add_argument("--no-wait", action="store_true", help="exit at the daily cap instead of waiting for tomorrow")
    st.set_defaults(fn=cmd_stories)

    p = sub.add_parser("phones", help="Trestle Real Contact on the heirs' numbers (dry; --commit pays)")
    common(p)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--max-pairs", type=int, default=250)
    p.add_argument("--only", default="")
    p.set_defaults(fn=cmd_phones)

    pe = sub.add_parser("people", help="one people-search lookup for a research session (capped per house)")
    common(pe)
    pe.add_argument("--house", required=True)
    pe.add_argument("--first", required=True)
    pe.add_argument("--last", required=True)
    pe.add_argument("--middle", default="")
    pe.add_argument("--street", default="")
    pe.add_argument("--city", default="")
    pe.add_argument("--state", default="")
    pe.add_argument("--zip", default="")
    pe.add_argument("--dob-year", default="")
    pe.add_argument("--max", type=int, default=3)
    pe.set_defaults(fn=cmd_people)

    w = sub.add_parser("widen", help="pull county houses SiftMap flags for a death or an estate into your account")
    common(w, county=True)
    w.add_argument("--commit", action="store_true")
    w.add_argument("--approve", type=int, default=0, help="how many new records you approve the pull to use")
    w.add_argument("--max", type=int, default=2000)
    w.set_defaults(fn=cmd_widen)

    wb = sub.add_parser("workbook", help="write the workbook again from the saved scores")
    common(wb)
    wb.set_defaults(fn=cmd_workbook)

    nx = sub.add_parser("next", help="where a market stands and what to run next")
    common(nx)
    nx.set_defaults(fn=cmd_next)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
