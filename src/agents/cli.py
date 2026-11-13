"""SiftStack agents command line.

    python src/agents/cli.py serve                       # the brain (webhooks + inline worker)
    python src/agents/cli.py status                      # queue, spend, recent tasks
    python src/agents/cli.py run --uuid <record uuid>    # run the deep prospect now, in this process
    python src/agents/cli.py run --address "123 Example St"
    python src/agents/cli.py setup-datasift              # the app steps + whether DataSift's Test arrived
    python src/agents/cli.py setup-datasift --staff-api [--commit]   # our accounts: webhook + sequence by API
    python src/agents/cli.py selftest                    # zero network, safe any time

    python src/agents/cli.py team                        # validate team/roster.yaml, regenerate team/TEAM.md
    python src/agents/cli.py hello                       # meet-the-team, locally (free, no model)
    python src/agents/cli.py ask "Roma, quick screen 123 Main St"   # one employee turn, in this process
    python src/agents/cli.py slack-manifest              # the Slack app manifest + one-click create link

    python src/agents/cli.py autopilot check [--live]    # validate team/autopilot.yaml (--live: presets exist)
    python src/agents/cli.py autopilot plan [stage ...]  # every stage's plan against the live account, read-only
    python src/agents/cli.py autopilot run <stage>       # one stage now, obeying its mode (plan writes nothing)
    python src/agents/cli.py autopilot batches           # outreach batches waiting on a person
    python src/agents/cli.py autopilot release <id> [--commit] [--uploaded]
    python src/agents/cli.py autopilot runs | undo <run id> [--commit]   # put a live run back
    python src/agents/cli.py autopilot lanes | budget | log              # timelines, request budget, log reach
    python src/agents/cli.py autopilot calibrate [days]                  # do the phone tiers predict what callers find
    python src/agents/cli.py schedule [--next 3]         # when every scheduled workflow fires next

AGENTS_MODE=dry (default) spends nothing and writes nothing to the CRM; AGENTS_MODE=live spends
within AGENTS_RUN_CAP_USD / AGENTS_DAILY_CAP_USD and writes back. An autopilot stage acts only
when its playbook mode is live AND the box runs AGENTS_MODE=live.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import config, store  # noqa: E402


def cmd_serve(a) -> int:
    import uvicorn
    uvicorn.run("agents.brain:app", host="0.0.0.0", port=int(os.environ.get("PORT") or 8080), log_level="info")
    return 0


def cmd_status(a) -> int:
    from datetime import datetime, timezone
    store.init()
    day = datetime.now(timezone.utc).isoformat()[:10]
    print("mode %s | tasks %s | spent today $%.2f of $%.0f" % (
        config.MODE, store.counts(), store.spent(day=day), config.DAILY_CAP_USD))
    for t in store.recent(10):
        print("  #%s %s %-14s %-8s %s" % (t["id"], t["created"][:16], t["workflow"], t["status"],
                                         (t["error"] or "")[:80]))
    return 0


def cmd_run(a) -> int:
    from agents import worker
    store.init()
    uuid = a.uuid
    if not uuid:
        import crm_key_client
        hits = crm_key_client.from_env().search_by_address(a.address, limit=5)
        if len(hits) != 1:
            print("%d records match %r; pass the full street line or --uuid" % (len(hits), a.address))
            return 2
        uuid = hits[0]["uuid"]
    task_id, _ = store.enqueue("research", "deep_prospect", uuid, subject=a.address or "", force=True)
    task = store.claim_next()
    while task and task["id"] != task_id:       # older queued work runs first, in order
        worker.run_task(task)
        task = store.claim_next()
    result = worker.run_task(task) if task else None
    print(json.dumps({k: v for k, v in (result or {}).items() if k != "log"}, indent=1, default=str))
    if result:
        print("\n".join(result.get("log", [])[-40:]))
    return 0 if result else 1


def cmd_setup(a) -> int:
    """Default: the app steps plus whether DataSift's Test delivery has arrived (exit 0 once it
    has, 2 while waiting on the person). --staff-api: our own accounts only, unsupported routes."""
    from agents import datasift_setup
    r = datasift_setup.safe_plan(a.commit, staff_api=a.staff_api)
    print(json.dumps(r, indent=1))
    if r.get("error"):
        return 1
    if r.get("mode") == "app":
        return 0 if r.get("verified") else 2
    return 1 if a.commit and not r.get("verified") else 0


def cmd_selftest(a) -> int:
    from agents import selftest
    return selftest.main()


def cmd_team(a) -> int:
    from agents import team, worker
    try:
        roster = team.load()
    except team.RosterError as exc:
        print(exc)
        return 1
    out = config.TEAM_DIR / "TEAM.md"
    text = ("<!-- GENERATED from team/roster.yaml by `python src/agents/cli.py team`. Do not edit by hand. -->\n\n"
            + team.render_team_md(roster))
    changed = not out.is_file() or out.read_text(encoding="utf-8") != text
    if changed and not a.check:
        out.write_text(text, encoding="utf-8")
    for e in roster.employees.values():
        owned = roster.owned(e.id)
        live = [w.name for w in owned if w.live]
        print("%-8s %-16s #%-16s %2d workflows, live: %s" % (e.display_name, e.role, e.slack_channel, len(owned),
                                                           ", ".join(live) or "none"))
    unwired = [w.name for w in roster.workflows.values() if w.live and not worker.handler_for(w.name)]
    for w in roster.warnings:
        print("warning: " + w)
    for w in unwired:
        print("problem: workflow %s is marked live but the worker has no handler for it" % w)
    print("TEAM.md %s" % ("is out of date" if (changed and a.check) else "written" if changed else "unchanged"))
    # The org chart (docs/agents.json): every live agent on it is run by some workflow here.
    agents = team.org_chart_agents()
    cov_problems, cov_stale = [], False
    if agents:
        cov = team.coverage(roster, agents)
        cov_problems = cov["problems"]
        cov_out = config.TEAM_DIR / "COVERAGE.md"
        cov_text = ("<!-- GENERATED from team/roster.yaml + docs/agents.json by `python src/agents/cli.py team`."
                    " Do not edit by hand. -->\n\n" + team.render_coverage_md(roster, agents))
        cov_stale = not cov_out.is_file() or cov_out.read_text(encoding="utf-8") != cov_text
        if cov_stale and not a.check:
            cov_out.write_text(cov_text, encoding="utf-8")
        print("org chart: %d agents, %d run by a workflow, %d platform, %d retired, %d unassigned; COVERAGE.md %s" % (
            cov["agents"], cov["covered"], len(cov["platform"]), len(cov["retired"]), len(cov["uncovered"]),
            "is out of date" if (cov_stale and a.check) else "written" if cov_stale else "unchanged"))
        for p in cov_problems:
            print("problem: " + p)
    return 1 if unwired or cov_problems or (a.check and (changed or cov_stale)) else 0


def _drain(limit: int = 50) -> int:
    from agents import worker
    n = 0
    while n < limit:
        task = store.claim_next()
        if not task:
            break
        worker.run_task(task)
        n += 1
    return n


def cmd_hello(a) -> int:
    from agents import hello, slack_app, team
    store.init()
    roster = team.load()
    hello.start(roster, slack_app.home_channel(None, roster), "", source="cli")
    print("ran %d task(s); without SLACK_BOT_TOKEN the posts above went to the webhook or the log" % _drain())
    return 0


def cmd_ask(a) -> int:
    from agents import team, worker
    store.init()
    roster = team.load()
    route = team.route_message(roster, a.channel, a.text)
    if route["kind"] != "ask":
        print("nobody is addressed; start with a name (%s)" % ", ".join(e.display_name for e in roster.employees.values()))
        return 2
    tid, _ = store.enqueue(route["agent"], "ask", "", subject=route["text"][:120],
                           payload={"text": route["text"], "channel_name": a.channel}, mode="agent", source="cli")
    print("queued task %d for %s; ran %d task(s)" % (tid, roster.employees[route["agent"]].display_name, _drain()))
    t = store.task(tid) or {}
    print("%s: %s" % (t.get("status"), (store.result_of(t).get("text") or t.get("question") or t.get("error") or "")[:2000]))
    return 0 if t.get("status") in ("done", "waiting") else 1


def cmd_slack_manifest(a) -> int:
    from agents import team
    url = a.url or config.PUBLIC_URL
    if not url:
        print("Pass --url https://<app>.fly.dev or set AGENTS_PUBLIC_URL: Slack checks the events URL on create.")
        return 2
    m = team.slack_manifest(url)
    print(json.dumps(m, indent=1))
    print("\nCreate the app in one click (Slack fills in every scope and event):\n" + team.manifest_url(m))
    return 0


def _print_summary(s) -> None:
    d = s.as_dict() if hasattr(s, "as_dict") else s
    tag = "LIVE" if d.get("live") else d.get("mode", "plan").upper()
    print("\n[%s] %s (%s, %s): %s" % (tag, d["stage"], d["kind"], d["owner"], d.get("head") or ""))
    print("    found %d, %s %d, spent $%.2f%s" % (
        d.get("found", 0), "acted" if d.get("live") else "would act on", d.get("acted", 0), d.get("usd", 0),
        (", would spend about $%.2f" % d["would_usd"]) if d.get("would_usd") else ""))
    for k, v in sorted((d.get("tally") or {}).items()):
        print("    - %s: %s" % (k, v))
    for ln in d.get("lines") or []:
        print("      %s" % ln)
    for p in d.get("problems") or []:
        print("    ! %s" % p)


def cmd_autopilot(a) -> int:
    from agents import team
    from agents.autopilot import ledger, mail, policy, sweep
    store.init()
    if a.action == "check":
        try:
            roster = team.load()
            pol = policy.load(employees=set(roster.employees))
        except (policy.PolicyError, team.RosterError) as exc:
            print(exc)
            return 1
        if pol is None:
            print("no team/autopilot.yaml on this install")
            return 1
        for st in pol.stages.values():
            print("%-18s %-12s %-9s %-5s %s" % (st.id, st.kind, roster.employees[st.owner].display_name, st.mode,
                                                "; ".join(st.schedule)))
        from agents.autopilot import kpi
        cat_problems = list(getattr(kpi.load(), "problems", []) or [])
        for ln in cat_problems:
            print("KPI catalog: %s" % ln)
        if cat_problems:
            return 1
        if a.live:
            from agents.autopilot.crm import CRM
            have = CRM().presets()
            missing = sorted({t for st in pol.stages.values() for t in st.presets + [p.get("preset") for p in
                              (st.get("pieces") or []) if isinstance(p, dict)] if t and t not in have})
            for t in missing:
                print("missing preset in the account: %r" % t)
            print("%d presets in the account, %d named by the playbook are missing" % (len(have), len(missing)))
            return 1 if missing else 0
        return 0
    if a.action == "dial-check":
        if len(a.stages) != 2:
            print("name the dial file and the stage: autopilot dial-check <file.csv> <stage_id>")
            return 2
        from agents.autopilot import dialexport
        print(json.dumps(dialexport.recheck(a.stages[0], a.stages[1]), indent=1, default=str))
        return 0
    if a.action == "plan":
        pol = policy.load()
        wanted = set(a.stages)
        from agents.autopilot.crm import CRM
        crm = CRM()
        out = []
        for sid, st in pol.stages.items():
            if (wanted and sid not in wanted) or st.mode == "off":
                continue
            try:
                s = sweep.run(sid, plan=True, crm=crm, pol=pol)
            except Exception as exc:  # noqa: BLE001
                from agents.autopilot.stages import Summary
                s = Summary(stage=sid, kind=st.kind, owner=st.owner, mode=st.mode, live=False)
                s.problem("%s: %s" % (type(exc).__name__, str(exc)[:300]))
            _print_summary(s)
            out.append(s.as_dict())
        print("\n%d API calls" % crm.calls)
        if a.json:
            Path(a.json).write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
            print("wrote %s" % a.json)
        return 0
    if a.action == "run":
        if not a.stages:
            print("name the stage to run")
            return 2
        _print_summary(sweep.run(a.stages[0], plan=a.plan))
        return 0
    if a.action == "mail-designs":
        from agents.autopilot.crm import CRM
        crm = CRM()
        try:
            tpl = crm.mail_service("/templates/")
            rows = tpl if isinstance(tpl, list) else (tpl.get("results") or tpl.get("data") or [])
            print("%d SiftMail design(s):" % len(rows))
            for t in rows:
                print("  design %-38s product %-5s %-18s %s" % (t.get("uuid") or t.get("id"), t.get("external_product_id"),
                                                              t.get("type") or t.get("template_type") or "",
                                                              t.get("name") or t.get("title") or ""))
        except Exception as exc:  # noqa: BLE001
            print("could not read SiftMail designs: %s" % str(exc)[:300])
        try:
            for p in crm.mail_pricing()[:20]:
                print("  price tier: %s" % json.dumps(p)[:160])
        except Exception as exc:  # noqa: BLE001
            print("could not read SiftMail pricing: %s" % str(exc)[:200])
        return 0
    if a.action == "backfill-sale-dates":
        from agents.autopilot import backfill
        if not a.csv:
            print("pass --csv with the scrape's run CSVs, e.g. --csv \"output/ftm_notices_2026-11-*.csv\"")
            return 2
        r = backfill.run(a.csv, commit=a.commit)
        print(json.dumps(r, indent=1))
        if not a.commit:
            print("DRY RUN, nothing written. Re-run with --commit to write %d sale date(s)." % r["to_write"])
        return 1 if r["problems"] else 0
    if a.action == "batches":
        for b in ledger.batches():
            print("#%-4s %s %-10s %-8s %4d pieces  $%.2f" % (b["id"], b["created"][:16], b["stage"], b["status"],
                                                           b["count"], b["usd"]))
        return 0
    if a.action == "release":
        if not a.stages:
            print("name the batch id")
            return 2
        from agents import release
        print(json.dumps(release.run(int(a.stages[0]), commit=a.commit, uploaded=a.uploaded), indent=1, default=str))
        return 0
    if a.action == "runs":
        for r in ledger.runs(int(a.stages[0]) if a.stages else 20):
            print("%-22s %-18s %s  %d change(s)%s" % (r["run_id"], r["stage"], r["at"][:16], r["n"],
                                                     "  (%d put back)" % r["undone"] if r["undone"] else ""))
        return 0
    if a.action == "undo":
        if not a.stages:
            print("name the run id (cli.py autopilot runs lists them)")
            return 2
        from agents.autopilot import undo
        r = undo.run(a.stages[0], commit=a.commit)
        print(json.dumps(r, indent=1, default=str))
        if r.get("dry"):
            print("DRY RUN, nothing changed. Re-run with --commit to put %d change(s) back." % r["would_undo"])
        return 0
    if a.action == "budget":
        from agents.autopilot import budget
        print(json.dumps(budget.usage(), indent=1))
        return 0
    if a.action == "lanes":
        from agents.autopilot import lanes
        pol = policy.load()
        for lane in lanes.ORDER:
            if pol.lanes.timeline(lane):
                print("%s" % lane)
                for line in lanes.describe(pol.lanes, lane):
                    print("  %s" % line)
        two = lanes.owners_in_two_lanes()
        print("%d owner(s) sit in two lanes (from the last owner_lanes run)" % len(two))
        return 0
    if a.action == "calibrate":
        from agents.autopilot import calibration
        from agents.autopilot.crm import CRM
        out = calibration.run(CRM(), days=int(a.stages[0]) if a.stages else 45)
        print(calibration.text(out))
        return 0
    if a.action == "last":
        # Each stage's last run in its own words. The public health page carries only the counts,
        # because these lines name streets; this is the operator's view (run it on the box over
        # `fly ssh console`, or locally for local runs).
        pol = policy.load()
        wanted = set(a.stages)
        for sid, st in pol.stages.items():
            if wanted and sid not in wanted:
                continue
            raw = store.kv_get("autopilot:last:%s" % sid)
            try:
                last = json.loads(raw) if raw else {}
            except ValueError:
                last = {}
            if not last:
                print("%-20s %-5s never run here" % (sid, st.mode))
                continue
            print("%-20s %-5s %s  found %s, %s %s" % (sid, st.mode, str(last.get("at", ""))[:16], last.get("found", 0),
                                                      "did" if last.get("live") else "would do", last.get("acted", 0)))
            if last.get("head"):
                print("    %s" % last["head"])
            for line in last.get("problems") or []:
                print("    ! %s" % line)
        return 0
    if a.action == "log":
        from agents.autopilot import activity
        for g in activity.GROUPS:
            thr = activity.synced_through(g)
            print("%-7s read through %s" % (g, thr.isoformat(timespec="minutes") if thr else "never"))
        return 0
    return 2


def cmd_schedule(a) -> int:
    from datetime import datetime, timezone
    from agents import scheduler, team
    roster = team.load()
    now = datetime.now(timezone.utc)
    for wf, owner, spec in scheduler.schedules(roster):
        nxt = ", ".join(s.strftime("%a %m-%d %H:%M") for s in scheduler.next_slots(spec, now, a.next))
        print("%-20s %-9s %-40s %s" % (wf.name, roster.employees[owner].display_name, spec.text, nxt))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SiftStack agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    au = sub.add_parser("autopilot")
    au.add_argument("action", choices=["check", "plan", "run", "batches", "release", "mail-designs", "dial-check",
                                       "backfill-sale-dates", "runs", "undo", "budget", "lanes", "log", "calibrate",
                                       "last"])
    au.add_argument("--csv", action="append", default=[], help="backfill-sale-dates: run CSV glob (repeatable)")
    au.add_argument("stages", nargs="*")
    au.add_argument("--live", action="store_true", help="check: confirm every preset title exists in the account")
    au.add_argument("--plan", action="store_true", help="run: force plan mode")
    au.add_argument("--json", default="", help="plan: also write the summaries to this file")
    au.add_argument("--commit", action="store_true", help="release, undo: actually write")
    au.add_argument("--uploaded", action="store_true", help="release: the CSV was uploaded to the mail house")
    sc = sub.add_parser("schedule")
    sc.add_argument("--next", type=int, default=3)
    sub.add_parser("serve")
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--uuid", default="")
    r.add_argument("--address", default="")
    s = sub.add_parser("setup-datasift")
    s.add_argument("--commit", action="store_true", help="--staff-api only: apply the writes")
    s.add_argument("--staff-api", action="store_true",
                   help="our accounts only: create the webhook and sequence through routes the published API no longer lists")
    sub.add_parser("selftest")
    t = sub.add_parser("team")
    t.add_argument("--check", action="store_true", help="fail if TEAM.md is stale instead of rewriting it")
    sub.add_parser("hello")
    k = sub.add_parser("ask")
    k.add_argument("text")
    k.add_argument("--channel", default="", help="channel name to route by, e.g. roma-sales")
    m = sub.add_parser("slack-manifest")
    m.add_argument("--url", default="")
    a = ap.parse_args(argv)
    if a.cmd == "run" and not (a.uuid or a.address):
        ap.error("run needs --uuid or --address")
    return {"serve": cmd_serve, "status": cmd_status, "run": cmd_run, "setup-datasift": cmd_setup,
            "selftest": cmd_selftest, "team": cmd_team, "hello": cmd_hello, "ask": cmd_ask,
            "slack-manifest": cmd_slack_manifest, "autopilot": cmd_autopilot, "schedule": cmd_schedule}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
