"""The dialer's web app.

    uvicorn dialer.app:app --host 0.0.0.0 --port 8080

Three kinds of route:
  * the caller's: /phone (the softphone page), /signin, and /api/* behind the session cookie. A request that
    changes anything must also carry X-Dialer: 1 (see auth.py).
  * Twilio's: /twilio/*. Every one is refused unless its X-Twilio-Signature matches the PUBLIC url Twilio
    called (behind Fly's proxy the app sees another), and every one answers from the database at once:
    a TwiML answer cannot wait on DataSift.
  * public by design: /media/vm/<token>.mp3 (Twilio fetches the voicemail drop by an unguessable name),
    /rec/<call>/<sig> and /vm/<call>/<sig> (signed links on DataSift notes), /hooks/handraise (the texting
    app, bearer token), /health (liveness only, never the database) and /stats (counts, no secrets).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, calls, config, dispositions, events, links, phone, queue, sift, store, texting, twilio_voice, writeback

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="SiftStack dialer", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
       "connect-src 'self' https://*.twilio.com wss://*.twilio.com; frame-ancestors 'none'; base-uri 'none'; "
       "form-action 'self'")


@app.middleware("http")
async def _headers(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if not p.startswith("/twilio/"):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
    if p.startswith("/api/") or p in ("/phone", "/signin"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store.init()
    for p in config.problems():
        log.warning("setup: %s", p)
    if config._env("DIALER_WORKER", "1") not in ("0", "false", "no", "off"):
        writeback.start_thread()
    log.info("dialer up | calls=%s writeback=%s record=%s amd=%s", config.S.calls, config.S.writeback,
             config.S.record, config.S.amd)


# ---------------------------------------------------------------------- helpers
def me(request: Request) -> dict | None:
    ident = auth.identity_of(request.cookies.get(auth.COOKIE))
    return store.get_caller(ident) if ident else None


def _need(request: Request, write: bool = False) -> dict | Response:
    caller = me(request)
    if not caller:
        return JSONResponse({"ok": False, "error": "sign in again"}, status_code=401)
    if write and request.headers.get("x-dialer") != "1":
        return JSONResponse({"ok": False, "error": "refused"}, status_code=403)
    return caller


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _bearer(request: Request, expected: str) -> bool:
    got = request.headers.get("authorization", "")
    token = got[7:].strip() if got.lower().startswith("bearer ") else ""
    return bool(expected and token) and hmac.compare_digest(token, expected)


async def _twilio(request: Request) -> dict | None:
    """The form Twilio posted, or None when its signature does not match."""
    form = await request.form()
    if not config.S.validate_webhooks:
        return {k: form.get(k) for k in form.keys()}
    if not config.S.public_url:
        log.warning("refused a Twilio webhook: DIALER_PUBLIC_URL is not set, so nothing can be verified")
        return None
    full = config.S.public_url + request.url.path + (("?" + request.url.query) if request.url.query else "")
    if not twilio_voice.valid_signature(full, form, request.headers.get("x-twilio-signature", ""),
                                        request.headers.get("x-twilio-webhooks-signature-settings", "")):
        log.warning("refused a Twilio webhook on %s: bad signature", request.url.path)
        return None
    return {k: form.get(k) for k in form.keys()}


def _xml(twiml: str) -> Response:
    return Response(content=twiml, media_type="text/xml")


FORBIDDEN = Response(status_code=403)


# ---------------------------------------------------------------------- pages
@app.get("/")
def root() -> Response:
    return RedirectResponse("/phone")


@app.get("/signin")
def signin_page() -> Response:
    return FileResponse(STATIC / "signin.html", media_type="text/html")


@app.post("/api/signin")
async def api_signin(request: Request) -> Response:
    if request.headers.get("x-dialer") != "1":
        return FORBIDDEN
    body = await _json(request)
    ident, sid = auth.signin(str(body.get("code") or ""))
    if not ident:
        return JSONResponse({"ok": False, "error": "that link is used or expired; ask for a new one"}, status_code=400)
    resp = JSONResponse({"ok": True, "name": (store.get_caller(ident) or {}).get("name")})
    resp.set_cookie(value=sid, **auth.cookie_kwargs())
    return resp


@app.post("/api/signout")
def api_signout(request: Request) -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/phone")
def phone_page(request: Request) -> Response:
    if not me(request):
        return FileResponse(STATIC / "signin.html", media_type="text/html")
    return FileResponse(STATIC / "phone.html", media_type="text/html")


# ---------------------------------------------------------------------- the caller's API
@app.get("/api/me")
def api_me(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    out = {"ok": True, "identity": c["identity"], "name": c["name"], "role": c["role"],
           "caller_id": c.get("caller_id") or "", "caller_id_pretty": phone.pretty(c.get("caller_id")),
           "vm_ready": bool(c.get("vm_file") and c.get("vm_token")),
           "settings": {"calls": config.S.calls, "writeback": config.S.writeback, "record": config.S.record,
                        "amd": config.S.amd, "vm_drop": config.S.vm_drop, "advance_seconds": config.S.advance_seconds,
                        "edge": config.S.edge, "twilio_ready": config.S.twilio_ready, "texting": texting.configured()},
           "dispositions": [{"key": r.key, "label": r.label, "group": r.group, "needs_note": r.needs_note,
                             "task": r.task, "contact": r.contact} for r in dispositions.TABLE.values()]}
    if c["role"] == "manager":
        out["problems"] = config.problems()
    return out


@app.get("/api/phone-token")
def api_token(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    if not config.S.twilio_ready:
        return JSONResponse({"ok": False, "error": "Twilio is not set up on this dialer yet"}, status_code=503)
    store.update_caller(c["identity"], seen_at=store.now())
    return {"ok": True, "token": twilio_voice.token(c["identity"]), "identity": c["identity"], "ttl": 3600}


@app.get("/api/events")
async def api_events(request: Request) -> Response:
    c = me(request)
    if not c:
        return JSONResponse({"ok": False}, status_code=401)
    ident = c["identity"]
    try:
        last = int(request.headers.get("last-event-id") or request.query_params.get("since") or 0)
    except ValueError:
        last = 0
    if not last:
        last = events.HUB.latest()

    async def stream():
        nonlocal last
        beat = 0.0
        yield "retry: 2000\n\n"
        while True:
            if await request.is_disconnected():
                return
            got = events.HUB.since(ident, last)
            for seq, kind, data, _ in got:
                last = seq
                yield "id: %d\nevent: %s\ndata: %s\n\n" % (seq, kind, json.dumps(data))
            await asyncio.sleep(0.35)
            beat += 0.35
            if beat >= 15:
                beat = 0.0
                yield ": keepalive\n\n"
                if not auth.identity_of(request.cookies.get(auth.COOKIE)):
                    return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/call/prepare")
async def api_prepare(request: Request) -> Any:
    c = _need(request, write=True)
    if isinstance(c, Response):
        return c
    b = await _json(request)
    kind = "queue" if b.get("item_id") else "manual"
    row = None
    if b.get("item_id"):
        item = store.get_item(int(b["item_id"]))
        if not item or item.get("claimed_by") != c["identity"]:
            return JSONResponse({"ok": False, "stop": ["that record is not yours in this session"]}, status_code=409)
        row = json.loads(item.get("row") or "{}")
    res = calls.prepare(c["identity"], str(b.get("number") or ""), str(b.get("record_uuid") or ""), kind=kind,
                        session_id=b.get("session_id"), item_id=b.get("item_id"), confirm=bool(b.get("confirm")), row=row)
    return res


@app.get("/api/call/{call_id}")
def api_call(call_id: str, request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    call = calls._mine(call_id, c["identity"])
    if not call:
        return JSONResponse({"ok": False, "error": "not your call"}, status_code=404)
    return {"ok": True, "call": calls._public(call)}


@app.post("/api/call/{call_id}/{action}")
async def api_call_action(call_id: str, action: str, request: Request) -> Any:
    c = _need(request, write=True)
    if isinstance(c, Response):
        return c
    b = await _json(request)
    ident = c["identity"]
    if action == "hangup":
        return calls.hangup(call_id, ident)
    if action == "vmdrop":
        return calls.vm_drop(call_id, ident)
    if action == "hold":
        return calls.hold(call_id, ident, bool(b.get("on")))
    if action == "transfer":
        return calls.transfer(call_id, ident, str(b.get("identity") or ""), str(b.get("number") or ""))
    if action == "dispose":
        return calls.dispose(call_id, ident, str(b.get("key") or ""), str(b.get("note") or ""), str(b.get("callback_at") or ""))
    if action == "accepted":
        calls.answered_inbound(call_id, ident)
        return {"ok": True}
    if action == "monitor":
        return calls.monitor_allowed(call_id, ident)
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)


@app.get("/api/record/{uuid}")
def api_record(uuid: str, request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    try:
        rec = sift.client().record(uuid)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "DataSift could not be read: %s" % str(exc)[:100]}, status_code=502)
    if not rec.get("uuid"):
        return JSONResponse({"ok": False, "error": "no such record"}, status_code=404)
    return {"ok": True, "record": sift.summary(rec)}


@app.get("/api/lookup")
def api_lookup(request: Request, number: str = "") -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    try:
        hits = sift.client().find_by_phone(number)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=502)
    return {"ok": True, "records": [sift.summary(h["record"], h["row"]) for h in hits]}


@app.get("/api/thread")
def api_thread(request: Request, number: str = "") -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    n = phone.digits(number)
    return {"ok": True, "configured": texting.configured(), "thread": texting.thread(n) if n else {}}


@app.get("/api/presets")
def api_presets(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    try:
        return {"ok": True, "presets": queue.presets()}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "DataSift could not be read: %s" % str(exc)[:100]}, status_code=502)


@app.get("/api/session/current")
def api_session_current(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    s = store.open_session_for(c["identity"])
    return {"ok": True, "session": queue.describe(s["id"]) if s else None}


@app.post("/api/session/start")
async def api_session_start(request: Request) -> Any:
    c = _need(request, write=True)
    if isinstance(c, Response):
        return c
    b = await _json(request)
    try:
        return queue.start(c["identity"], str(b.get("preset") or ""))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "the preset could not be read: %s" % str(exc)[:120]}, status_code=502)


@app.post("/api/session/{session_id}/{action}")
async def api_session_action(session_id: int, action: str, request: Request) -> Any:
    c = _need(request, write=True)
    if isinstance(c, Response):
        return c
    b = await _json(request)
    if action == "next":
        return queue.next_item(session_id, c["identity"])
    if action == "skip":
        return queue.skip(int(b.get("item_id") or 0), c["identity"], str(b.get("reason") or "skipped by the caller"))
    if action == "stop":
        return queue.stop(session_id, c["identity"])
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=404)


@app.get("/api/handraises")
def api_handraises(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    rows = store.open_handraises()
    return {"ok": True, "handraises": [{"id": r["id"], "number": r["phone"], "pretty": phone.pretty(r["phone"]),
                                        "record_uuid": r.get("record_uuid") or "", "body": r.get("body") or "",
                                        "program": r.get("program") or "", "at": r["received_at"]} for r in rows]}


@app.get("/api/recent")
def api_recent(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    from . import rules
    rows = store.recent_calls(c["identity"], 40, since=rules.day_start_utc())
    return {"ok": True, "calls": [calls._public(r) for r in rows]}


@app.get("/api/active")
def api_active(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    if c["role"] != "manager":
        return JSONResponse({"ok": False, "error": "managers only"}, status_code=403)
    out = []
    for r in store.active_calls():
        pub = calls._public(r)
        pub["caller"] = calls._caller_name(r.get("identity") or "")
        out.append(pub)
    return {"ok": True, "calls": out}


@app.get("/api/team")
def api_team(request: Request) -> Any:
    c = _need(request)
    if isinstance(c, Response):
        return c
    return {"ok": True, "team": [{"identity": t["identity"], "name": t["name"], "role": t["role"]}
                                 for t in store.callers() if t["identity"] != c["identity"]]}


@app.get("/api/vm/preview")
def api_vm_preview(request: Request) -> Response:
    c = _need(request)
    if isinstance(c, Response):
        return c
    if not c.get("vm_file"):
        return Response(status_code=404)
    p = config.S.data_dir / "media" / c["vm_file"]
    return FileResponse(p, media_type="audio/mpeg") if p.exists() else Response(status_code=404)


@app.post("/api/vm/upload")
async def api_vm_upload(request: Request) -> Any:
    """A voicemail drop uploaded as an mp3 (Twilio's <Play> takes mp3 and wav; a browser recording is webm)."""
    c = _need(request, write=True)
    if isinstance(c, Response):
        return c
    raw = await request.body()
    if not raw or len(raw) > 3 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "send an mp3 under 3 MB"}, status_code=400)
    is_mp3 = raw[:3] == b"ID3" or (raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)
    if not is_mp3:
        return JSONResponse({"ok": False, "error": "that file is not an mp3"}, status_code=400)
    import secrets as _s
    import time as _t
    folder = config.S.data_dir / "media"
    folder.mkdir(parents=True, exist_ok=True)
    name = "vm_%s_%d.mp3" % (c["identity"], int(_t.time()))
    (folder / name).write_bytes(raw)
    store.update_caller(c["identity"], vm_file=name, vm_token=_s.token_urlsafe(18))
    return {"ok": True}


# ---------------------------------------------------------------------- Twilio
@app.post("/twilio/voice")
async def tw_voice(request: Request, background: BackgroundTasks) -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    twiml, follow = calls.voice_twiml(params)
    if follow:
        background.add_task(follow)
    return _xml(twiml)


@app.post("/twilio/status")
async def tw_status(request: Request, call_id: str = "", leg: str = "lead") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_status(call_id, leg, params)
    return Response(status_code=204)


@app.post("/twilio/conference")
async def tw_conference(request: Request, call_id: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_conference(call_id, params)
    return Response(status_code=204)


@app.post("/twilio/amd")
async def tw_amd(request: Request, call_id: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_amd(call_id, params)
    return Response(status_code=204)


@app.post("/twilio/recording")
async def tw_recording(request: Request, call_id: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_recording(call_id, params)
    return Response(status_code=204)


@app.post("/twilio/inbound")
async def tw_inbound(request: Request) -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    return _xml(calls.inbound_twiml(params))


@app.post("/twilio/inbound_done")
async def tw_inbound_done(request: Request, call_id: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    return _xml(calls.inbound_done(call_id, params))


@app.post("/twilio/voicemail")
async def tw_voicemail(request: Request, call_id: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_voicemail(call_id, params)
    return Response(status_code=204)


@app.post("/twilio/vm_recorded")
async def tw_vm_recorded(request: Request, identity: str = "") -> Response:
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    calls.on_vm_recorded(identity, params)
    return Response(status_code=204)


@app.post("/twilio/agent_status")
async def tw_agent_status(request: Request) -> Response:
    """The TwiML App's own status callback: the caller's browser leg ended."""
    params = await _twilio(request)
    if params is None:
        return FORBIDDEN
    call = store.call_by_sid(str(params.get("CallSid") or ""))
    if call:
        store.add_event(call["id"], "twilio", "agent:" + str(params.get("CallStatus") or ""), calls._keep(params))
        if call.get("kind") in ("monitor", "vm_record") and str(params.get("CallStatus")) in calls.FINAL:
            store.update_call(call["id"], status="ended", ended_at=store.now())
        elif call.get("status") in ("prepared", "connecting") and str(params.get("CallStatus")) in calls.FINAL:
            store.add_job(call["id"], "reconcile", {}, delay_s=5)
    return Response(status_code=204)


# ---------------------------------------------------------------------- public by design
@app.get("/media/vm/{token}.mp3")
def media_vm(token: str) -> Response:
    caller = store.caller_by_vm_token(token)
    if not caller or not caller.get("vm_file"):
        return Response(status_code=404)
    p = config.S.data_dir / "media" / caller["vm_file"]
    return FileResponse(p, media_type="audio/mpeg") if p.exists() else Response(status_code=404)


def _stream_recording(sid: str) -> Response:
    try:
        audio = twilio_voice.voice().recording_bytes(sid, "mp3")
    except Exception as exc:  # noqa: BLE001
        log.info("recording %s unavailable: %s", sid[-6:], exc)
        return Response(status_code=404)
    return Response(content=audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/rec/{call_id}/{sig}")
def recording(call_id: str, sig: str) -> Response:
    if not links.ok("rec", call_id, sig):
        return Response(status_code=404)
    call = store.get_call(call_id)
    if not call or not call.get("recording_sid"):
        return Response(status_code=404)
    return _stream_recording(call["recording_sid"])


@app.get("/vm/{call_id}/{sig}")
def voicemail(call_id: str, sig: str) -> Response:
    if not links.ok("vm", call_id, sig):
        return Response(status_code=404)
    call = store.get_call(call_id)
    if not call or not call.get("voicemail_sid"):
        return Response(status_code=404)
    return _stream_recording(call["voicemail_sid"])


def recording_link(call_id: str) -> str:
    return links.recording_link(call_id)


@app.post("/hooks/handraise")
async def hook_handraise(request: Request) -> Response:
    """The texting app pushes a hand raise (an owner who texted back interested) to every caller's page."""
    if not _bearer(request, config.S.handraise_token):
        return Response(status_code=404)
    b = await _json(request)
    n = phone.digits(b.get("phone"))
    if not n:
        return JSONResponse({"ok": False, "error": "no phone"}, status_code=400)
    hid = store.add_handraise(n, str(b.get("record_uuid") or ""), str(b.get("owner_uuid") or ""),
                              str(b.get("body") or ""), str(b.get("program") or ""), "texting",
                              dedupe=str(b.get("id") or "") or None)
    if hid:
        events.HUB.publish_many([c["identity"] for c in store.callers()], "handraise",
                                {"id": hid, "number": n, "pretty": phone.pretty(n), "record_uuid": b.get("record_uuid") or "",
                                 "body": str(b.get("body") or "")[:300]})
    return JSONResponse({"ok": True, "id": hid, "duplicate": hid is None})


@app.get("/health")
def health() -> dict:
    """Liveness only. It never reads the database, so a slow query can never restart the machine."""
    return {"ok": True, "calls": config.S.calls, "writeback": config.S.writeback}


@app.get("/stats")
def stats() -> dict:
    from . import rules
    today = rules.day_start_utc()
    c = store.conn()
    n = lambda q, *a: int(c.execute(q, a).fetchone()[0])  # noqa: E731
    return {
        "ok": True, "calls": config.S.calls, "writeback": config.S.writeback, "record": config.S.record,
        "setup_problems": len(config.problems()),
        "today": {"dials": n("SELECT COUNT(*) FROM calls WHERE created_at>=? AND direction='outbound' AND status NOT IN"
                             " ('prepared','blocked') AND kind NOT IN ('monitor','vm_record')", today),
                  "answered": n("SELECT COUNT(*) FROM calls WHERE created_at>=? AND answered_at IS NOT NULL", today),
                  "blocked": n("SELECT COUNT(*) FROM calls WHERE created_at>=? AND status='blocked'", today),
                  "inbound": n("SELECT COUNT(*) FROM calls WHERE created_at>=? AND direction='inbound'", today)},
        "live_calls": len(store.active_calls()),
        "jobs": {"queued": n("SELECT COUNT(*) FROM jobs WHERE state='queued'"),
                 "failed": n("SELECT COUNT(*) FROM jobs WHERE state='failed'")},
        "undisposed_answered": n("SELECT COUNT(*) FROM calls WHERE answered_at IS NOT NULL AND disposition IS NULL AND"
                                 " status='ended' AND kind NOT IN ('monitor','vm_record')"),
        "worker_alive_at": store.kv_get("worker:alive"),
    }
