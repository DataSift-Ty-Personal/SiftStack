"""The brain: one always-on web process that turns DataSift sequence webhooks into agent tasks.

    POST /hooks/datasift/<secret>/<workflow>   a sequence's webhook action lands here
    GET  /health                               mode, queue, today's spend
    GET  /files/<task>/<sig>/<name>            a property file, signed, for the Slack link

The webhook answers in milliseconds and the work happens on the worker thread, because DataSift
documents no retry and a slow answer risks a dropped event. Every body is logged before routing.
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from . import config, router, slack, store, worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("agents.brain")

app = FastAPI(title="SiftStack agents", docs_url=None, redoc_url=None)
_worker = {}


@app.on_event("startup")
def _startup() -> None:
    store.init()
    n = store.requeue_stale()
    if n:
        log.info("requeued %d task(s) interrupted by a restart", n)
    if config.INLINE_WORKER:
        _worker["thread"], _worker["stop"] = worker.start_thread()


@app.get("/health")
def health() -> dict:
    day = datetime.now(timezone.utc).isoformat()[:10]
    return {"ok": True, "mode": config.MODE, "tasks": store.counts(),
            "spent_today_usd": round(store.spent(day=day), 2), "daily_cap_usd": config.DAILY_CAP_USD,
            "run_cap_usd": config.RUN_CAP_USD,
            "worker_alive": bool(_worker.get("thread") and _worker["thread"].is_alive())}


@app.post("/hooks/datasift/{secret}/{workflow}")
async def datasift_hook(secret: str, workflow: str, request: Request) -> dict:
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(secret, config.WEBHOOK_SECRET):
        raise HTTPException(status_code=404)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {"_raw": (await request.body()).decode("utf-8", "replace")[:20000]}
    event_id = store.log_event("datasift", workflow, payload)
    try:
        r = router.route(workflow, payload)
    except KeyError:
        raise HTTPException(status_code=404) from None
    if not r["record_uuid"]:
        slack.post(":warning: A DataSift webhook for *%s* arrived with no record id I could read. "
                   "Logged as event %s; nothing was run." % (workflow, event_id))
        return {"ok": True, "queued": False, "event": event_id, "reason": "no record id"}
    task_id, created = store.enqueue(r["agent"], r["workflow"], r["record_uuid"], event_id=event_id)
    return {"ok": True, "queued": created, "task": task_id, "event": event_id}


@app.get("/files/{task_id}/{sig}/{name}")
def files(task_id: int, sig: str, name: str):
    if not hmac.compare_digest(sig, slack.sign(task_id, name)) or "/" in name or "\\" in name:
        raise HTTPException(status_code=404)
    t = store.task(task_id)
    run_dir = ((t or {}).get("result") or "")
    try:
        import json
        run_dir = json.loads(run_dir).get("run_dir", "")
    except ValueError:
        run_dir = ""
    path = Path(run_dir) / "packs" / name if run_dir else None
    if not path or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=name)
