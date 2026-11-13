"""The brain: one always-on web process that turns Slack messages, DataSift sequence webhooks and
the deploy's smoke test into tasks for the team.

    POST /slack/events                         the Slack app's event subscription (signed)
    POST /hooks/datasift/<secret>/<workflow>   a sequence's webhook action lands here
    POST /team/hello/<secret>                  start meet-the-team in the team channel (deploy check)
    GET  /health                               mode, team, queue, today's spend
    GET  /files/<task>/<sig>/<name>            a property file, signed, for the Slack link

Every route answers in milliseconds and the work happens on the worker thread: the web process
never runs a model, because Slack retries after 3 seconds and DataSift retries a slow or failed
delivery up to three times. DataSift deliveries are Ed25519-signed and verified before anything is
logged (webhook_sig); each is deduped on DataSift's own event_id, then logged before routing.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from . import config, hello, intake, router, slack, slack_app, store, team, worker

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
    if config.SCHEDULER:
        from . import scheduler
        _worker["clock"], _worker["clock_stop"] = scheduler.start_thread(_roster_or_none)
        log.info("scheduler on: live workflows with a schedule trigger fire at business-local time")
    if not first_hello() and hello_pending():
        _worker["hello_stop"] = threading.Event()
        _worker["hello"] = threading.Thread(target=hello_until_landed, args=(_worker["hello_stop"],),
                                            name="first-hello", daemon=True)
        _worker["hello"].start()


FIRST_HELLO_KEY = "slack:first_hello"


def hello_pending() -> bool:
    """The Slack app's keys are set and the team has not introduced itself yet."""
    return slack_app.mode() == "bot" and not store.kv_get(FIRST_HELLO_KEY)


def hello_until_landed(stop, every_s: float = 300.0, tries: int = 288) -> int:
    """Keep trying the first hello, every five minutes for a day. Two real cases: the channel is
    private and the app is invited a few minutes after its keys were pasted, or Slack was not
    reachable when the brain started. Stops the moment it lands. Returns the tries it used."""
    used = 0
    while used < tries and hello_pending():
        if stop.wait(every_s):
            break
        used += 1
        if first_hello():
            break
    return used


def first_hello() -> int:
    """The first boot with a working Slack app has the team introduce itself, once. The person who
    just pasted the app's two keys sees it worked without asking anyone, and nobody has to call
    /team/hello. It is marked done only when the opening message really landed, so a wrong key or
    a channel the app cannot reach is tried again on the next boot. Returns the task id, or 0."""
    try:
        if slack_app.mode() != "bot" or store.kv_get(FIRST_HELLO_KEY):
            return 0
        roster = _roster_or_none()
        if not roster:
            return 0
        tid = hello.start(roster, slack_app.home_channel(None, roster), "", source="first-boot", must_land=True)
        if tid:
            store.kv_set(FIRST_HELLO_KEY, store.now())
            log.info("slack is connected: the team is introducing itself (task %s)", tid)
        else:
            log.warning("slack app keys are set but the first message did not land; trying again next boot")
        return tid
    except Exception as exc:  # noqa: BLE001  (a greeting must never stop the brain from starting)
        log.warning("first hello failed: %s", exc)
        return 0


def _roster_or_none():
    try:
        return team.load()
    except Exception as exc:  # noqa: BLE001
        log.error("roster failed to load: %s", exc)
        return None


@app.get("/health")
def health() -> dict:
    day = datetime.now(timezone.utc).isoformat()[:10]
    roster = _roster_or_none()
    return {"ok": True, "mode": config.MODE, "tasks": store.counts(),
            "spent_today_usd": round(store.spent(day=day), 2), "daily_cap_usd": config.DAILY_CAP_USD,
            "run_cap_usd": config.RUN_CAP_USD, "slack": slack_app.mode(), "claude": config.USE_LLM,
            "slack_one_channel": bool(config.SLACK_CHANNEL), "slack_first_hello": store.kv_get(FIRST_HELLO_KEY),
            "team": {e.id: e.display_name for e in roster.employees.values()} if roster else None,
            "live_workflows": sorted(w.name for w in roster.workflows.values() if w.live) if roster else [],
            "worker_alive": bool(_worker.get("thread") and _worker["thread"].is_alive()),
            "clock_alive": bool(_worker.get("clock") and _worker["clock"].is_alive()),
            "datasift_webhook_tests": {slug: store.kv_get("datasift:webhook_test:%s" % slug)
                                       for slug in router.routes(roster)},
            "autopilot": _autopilot_health()}


PUBLIC_LAST = ("at", "live", "found", "acted", "usd", "would_usd")


def public_last(last: dict) -> dict:
    """What the PUBLIC health page may say about a stage's last run: when, and numbers. Never the
    run's own sentences. A headline or a problem line names streets ("no confirmed number, for
    example 12 Oak St"), and this page answers anyone who asks. The sentences are one command
    away for the operator: `cli.py autopilot last`."""
    out = {k: last[k] for k in PUBLIC_LAST if k in last}
    if "problems" in last:
        out["problems"] = len(last.get("problems") or [])
    return out


def _autopilot_health() -> dict:
    """Each playbook stage: its mode and when its last run was, with its counts. A stage that
    stopped running shows an old `at`, which is what an outside watchdog looks for."""
    try:
        from .autopilot import policy
        pol = policy.load()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}
    if pol is None:
        return {}
    out = {}
    try:
        from .autopilot import activity, budget
        out["_requests"] = budget.usage()
        out["_log_read_through"] = {g: (activity.synced_through(g).isoformat(timespec="minutes")
                                         if activity.synced_through(g) else "") for g in activity.GROUPS}
    except Exception as exc:  # noqa: BLE001
        out["_requests"] = {"error": str(exc)[:120]}
    for sid, st in pol.stages.items():
        raw = store.kv_get("autopilot:last:%s" % sid)
        try:
            last = json.loads(raw) if raw else {}
        except ValueError:
            last = {}
        out[sid] = {"mode": st.mode, "owner": st.owner, "last": public_last(last)}
    return out


def _secret_ok(secret: str) -> bool:
    return bool(config.WEBHOOK_SECRET) and hmac.compare_digest(secret, config.WEBHOOK_SECRET)


@app.post("/slack/events")
async def slack_events(request: Request) -> dict:
    # Ingress stays closed until the Slack app's signing secret is set: no secret, no listener.
    if not config.SLACK_SIGNING_SECRET:
        raise HTTPException(status_code=404)
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not slack_app.verify(headers, raw):
        raise HTTPException(status_code=401)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400) from None
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}
    if headers.get("x-slack-retry-num"):
        return {"ok": True, "retry": True}     # the first delivery was acked; a retry is a duplicate
    event = body.get("event") or {}
    if body.get("event_id") and not store.seen_once("slack:%s" % body["event_id"]):
        return {"ok": True, "duplicate": True}
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True, "ignored": True}   # our own posts, edits, joins
    roster = _roster_or_none()
    if not roster:
        return {"ok": False, "error": "roster"}
    store.log_event("slack", event.get("type", ""), {"channel": event.get("channel"), "ts": event.get("ts"),
                                                     "thread_ts": event.get("thread_ts")})
    return {"ok": True, **intake.slack_event(event, roster)}


@app.post("/team/hello/{secret}")
def team_hello(secret: str) -> dict:
    if not _secret_ok(secret):
        raise HTTPException(status_code=404)
    roster = _roster_or_none()
    if not roster:
        raise HTTPException(status_code=500, detail="roster did not load")
    tid = hello.start(roster, slack_app.home_channel(None, roster), "", source="deploy")
    if slack_app.mode() == "bot" and not store.kv_get(FIRST_HELLO_KEY):
        store.kv_set(FIRST_HELLO_KEY, store.now())
    return {"ok": True, "task": tid}


@app.post("/hooks/datasift/{secret}/{workflow}")
async def datasift_hook(secret: str, workflow: str, request: Request) -> dict:
    if not _secret_ok(secret):
        raise HTTPException(status_code=404)
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        payload = {"_raw": raw.decode("utf-8", "replace")[:20000]}
    # Every DataSift delivery is Ed25519-signed (developers.datasift.ai/guides/webhooks). In require
    # mode an unsigned or forged body is refused before anything is logged or queued.
    from . import webhook_sig
    sig_mode = webhook_sig.mode()
    if sig_mode != "off":
        ok, why = webhook_sig.verify(request.headers, raw)
        if not ok:
            log.warning("datasift webhook signature %s: %s", "REFUSED" if sig_mode == "require" else "invalid", why)
            if sig_mode == "require":
                raise HTTPException(status_code=401, detail="signature: %s" % why)
    if isinstance(payload, dict) and payload.get("event_type") == "webhook.test":
        store.log_event("datasift", workflow + ":test", payload)
        store.kv_set("datasift:webhook_test:%s" % workflow, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return {"ok": True, "test": True}
    # DataSift retries up to three times and may deliver twice or out of order: its event_id is the key.
    ds_event = (payload.get("event_id") if isinstance(payload, dict) else "") or ""
    if ds_event and not store.seen_once("datasift-event:%s" % ds_event):
        return {"ok": True, "duplicate": True}
    event_id = store.log_event("datasift", workflow, payload)
    try:
        r = router.route(workflow, payload, _roster_or_none())
    except KeyError:
        raise HTTPException(status_code=404) from None
    if not r["record_uuid"]:
        slack_app.notice(":warning: A DataSift webhook for *%s* arrived with no record id I could read. "
                         "Logged as event %s; nothing was run." % (workflow, event_id), _roster_or_none())
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
