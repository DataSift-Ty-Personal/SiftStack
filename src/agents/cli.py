"""SiftStack agents command line.

    python src/agents/cli.py serve                       # the brain (webhooks + inline worker)
    python src/agents/cli.py status                      # queue, spend, recent tasks
    python src/agents/cli.py run --uuid <record uuid>    # run the deep prospect now, in this process
    python src/agents/cli.py run --address "123 Example St"
    python src/agents/cli.py setup-datasift [--commit]   # webhook + sequence, dry by default
    python src/agents/cli.py selftest                    # zero network, safe any time

AGENTS_MODE=dry (default) spends nothing and writes nothing to the CRM; AGENTS_MODE=live spends
within AGENTS_RUN_CAP_USD / AGENTS_DAILY_CAP_USD and writes back.
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
    from agents import datasift_setup
    r = datasift_setup.safe_plan(a.commit)
    print(json.dumps(r, indent=1))
    return 1 if r.get("error") or (a.commit and not r.get("verified")) else 0


def cmd_selftest(a) -> int:
    from agents import selftest
    return selftest.main()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SiftStack agents")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("--uuid", default="")
    r.add_argument("--address", default="")
    s = sub.add_parser("setup-datasift")
    s.add_argument("--commit", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "run" and not (a.uuid or a.address):
        ap.error("run needs --uuid or --address")
    return {"serve": cmd_serve, "status": cmd_status, "run": cmd_run, "setup-datasift": cmd_setup,
            "selftest": cmd_selftest}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
