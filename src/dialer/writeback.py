"""The write-back worker: what a disposition writes, done off the call path.

`plan()` queues the jobs for one disposed call; `run_forever()` (a thread inside the app) drains them.

  writeback   DataSift, in order: the attempt counter (once per record per business day), the phone's status
              and tags (one upsert carrying the WHOLE phone object, then read back), the record's status, its
              tags and lists, a call-back task, and a note. Each step is logged in `writes` with the value it
              replaced. A step that already landed live is never repeated when the job is retried, so a
              retry after a timeout cannot post a second note or create a second task.
  texting     the texting app is told about the call (it pauses or closes its own thread, cancels queued
              texts, queues the Smarter Contact label or do-not-contact); see texting.py.
  kpi         the call goes to the brain's KPI receiver in the flat "ours" shape, so the scoreboard counts it.
  slack       a lead or a missed call, said once in the team's channel.
  fetch_vm    a caller's voicemail-drop recording is copied from Twilio onto the volume.
  auto_dispose  a call nobody dispositioned in 15 minutes gets the obvious outcome (no answer, voicemail,
              dead number); an answered call is left for a person and said in /stats.
  reconcile   a call whose last callback never came is asked about.

DIALER_WRITEBACK=dry logs every DataSift step it would take and sends nothing; off skips DataSift writes
(the texting app, the KPI receiver and Slack still hear about the call, since those are this app's own).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, dispositions, phone, rules, sift, store

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
LEAD_STATUS = "new_lead"
NOTE_MIN_SECONDS = 60          # an answered call this long gets a note even when the caller typed nothing


def plan(call: dict) -> list[int]:
    """Queue the jobs for one disposed call. The attempt-counter decision is made HERE, once, and carried
    in the job, so a retry neither skips nor doubles it."""
    row = dispositions.TABLE.get(call.get("disposition") or "")
    if not row:
        return []
    jobs = []
    counts = bool(call.get("direction") == "outbound" and call.get("record_uuid") and call.get("lead_sid")
                  and call.get("kind") not in ("monitor", "vm_record"))
    first_today = False
    if counts:
        day = rules.business_day(store.parse(call.get("started_at") or call.get("created_at")))
        first_today = store.first_attempt_today(call["record_uuid"], day)
    if call.get("record_uuid"):
        # A recording takes a few seconds to finish after the call; the note waits a moment for its link.
        jobs.append(store.add_job(call["id"], "writeback", {"attempt": first_today}, delay_s=8))
    if config.S.texting_url and config.S.texting_token and call.get("number"):
        jobs.append(store.add_job(call["id"], "texting", {}))
    if config.S.kpi_url:
        jobs.append(store.add_job(call["id"], "kpi", {}))
    if row.notify and config.S.slack_webhook:
        jobs.append(store.add_job(call["id"], "slack", {"kind": "lead"}))
    return jobs


# ---------------------------------------------------------------------- the DataSift steps
def _done(call_id: str, action: str) -> bool:
    return any(w["action"] == action and w["ok"] and w["mode"] == "live" for w in store.writes_for(call_id))


def _phone(rec: dict, number: str) -> dict | None:
    for p in ((rec.get("owner") or {}).get("phones") or []):
        if sift.digits(p.get("number")) == number:
            return p
    return None


def _status_of(rec: dict) -> str:
    s = rec.get("status")
    if isinstance(s, dict):
        s = s.get("value") or s.get("title") or ""
    return str(s or "")


def note_text(call: dict, row) -> str:
    caller = (store.get_caller(call.get("identity") or "") or {}).get("name") or "Caller"
    when = store.parse(call.get("started_at") or call.get("created_at")) or datetime.now(timezone.utc)
    local = when.astimezone(ZoneInfo(config.S.business_tz))
    clock = local.strftime("%b %d %I:%M%p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm")
    secs = int(call.get("seconds") or 0)
    talk = ("%dm %02ds" % divmod(secs, 60)) if secs else "no answer"
    way = "Call from" if call.get("direction") == "inbound" else "Call to"
    parts = ["%s %s, %s, %s: %s." % (way, phone.pretty(call.get("number")), clock, caller, row.label)]
    if secs:
        parts[0] = "%s %s, %s, %s, %s: %s." % (way, phone.pretty(call.get("number")), clock, caller, talk, row.label)
    if call.get("callback_at"):
        cb = store.parse(call["callback_at"]).astimezone(ZoneInfo(config.S.business_tz))
        parts.append("Call back %s." % cb.strftime("%a %b %d %I:%M%p").replace(" 0", " ").replace("AM", "am").replace("PM", "pm"))
    if call.get("vm_dropped"):
        parts.append("Voicemail left.")
    if call.get("note"):
        parts.append("Note: %s" % call["note"])
    if call.get("recording_sid"):
        from .links import recording_link
        parts.append("Recording: %s" % recording_link(call["id"]))
    parts.append("(SiftStack dialer)")
    return " ".join(parts)


def run_writeback(job: dict, call: dict) -> dict:
    mode = config.S.writeback
    row = dispositions.TABLE.get(call.get("disposition") or "")
    payload = json.loads(job.get("payload") or "{}")
    if mode == "off" or not row:
        return {"skipped": "write-back is off" if mode == "off" else "no outcome"}
    # The note carries the recording's link: give the recording a little time to arrive.
    ended = store.parse(call.get("ended_at"))
    if (call.get("record_allowed") and call.get("answered_at") and not call.get("recording_sid") and ended
            and (datetime.now(timezone.utc) - ended).total_seconds() < 120):
        raise RetryLater("waiting for the recording", 15)
    s = sift.client()
    uuid = call["record_uuid"]
    rec = s.record(uuid, fresh=True, interactive=False)
    if not rec.get("uuid"):
        raise RuntimeError("the record could not be read")
    owner = rec.get("owner") or {}
    owner_uuid = owner.get("uuid") or ""
    did, skipped = [], []

    def step(action: str, target: str, before, after, fn, verify=None):
        if _done(call["id"], action):
            skipped.append(action)
            return
        if mode == "dry":
            store.add_write(call["id"], job["id"], target, action, before, after, True, "dry")
            did.append(action)
            return
        try:
            fn()
        except Exception as exc:
            store.add_write(call["id"], job["id"], target, action, before, after, False, "live", detail=str(exc)[:300])
            raise
        ok = verify() if verify else None
        store.add_write(call["id"], job["id"], target, action, before, after, True, "live", verified=ok)
        did.append(action)

    # 1. The attempt counter: the presets move a record from Ready to Call to Call Attempt 1 on it.
    if payload.get("attempt"):
        total = payload.get("total")
        if total is None:
            total = int(rec.get("predictivecall_attempts") or 0) + 1
            payload["total"] = total
            store_job_payload(job["id"], payload)
        before = int(rec.get("predictivecall_attempts") or 0)
        if before < total:
            step("call_attempts", "record:%s" % uuid, before, total, lambda: s.call_attempts(uuid, total))

    # 2. The phone: status and tags in ONE upsert of the whole object, then read back.
    p = _phone(rec, call.get("number") or "")
    if p is not None and owner_uuid:
        cur = str(p.get("status") or "UNKNOWN").upper()
        want_status = dispositions.phone_target(row, cur)
        tags = sift.titles(p.get("tags"))
        add_tags = [t for t in row.phone_tags if t not in tags]
        if want_status or add_tags:
            payload_phone = sift.phone_payload(p, **({"status": want_status} if want_status else {}),
                                               **({"tags": tags + add_tags} if add_tags else {}))

            def verify_phone():
                s.forget(uuid)
                back = _phone(s.record(uuid, fresh=True, interactive=False), call["number"]) or {}
                ok = True
                if want_status:
                    ok = ok and str(back.get("status") or "").upper() == want_status
                if add_tags:
                    ok = ok and all(t in sift.titles(back.get("tags")) for t in add_tags)
                return ok
            step("phone", "phone:%s" % phone.last4(call["number"]), {"status": cur, "tags": tags},
                 {"status": want_status or cur, "tags": tags + add_tags},
                 lambda: s.upsert_phone(owner_uuid, payload_phone), verify_phone)

    # 3. The record's status, never moved down.
    want = dispositions.status_target(row, _status_of(rec), LEAD_STATUS)
    if want:
        step("status", "record:%s" % uuid, _status_of(rec), want, lambda: s.set_status(uuid, want))

    # 4. Tags and lists, one title per call (a comma string would make one tag with commas in its name).
    have = set(sift.titles(rec.get("tags")))
    for t in row.tags:
        if t not in have:
            step("tag:" + t, "record:%s" % uuid, None, t, (lambda t=t: s.add_tag(uuid, t)))
    lists = set(sift.titles(rec.get("lists")))
    for li in row.lists:
        if li not in lists:
            step("list:" + li, "record:%s" % uuid, None, li, (lambda li=li: s.add_list(uuid, li)))

    # 5. A call-back task for the caller.
    if row.task and call.get("callback_at"):
        caller = store.get_caller(call.get("identity") or "") or {}
        title = "Call back %s" % (sift.summary(rec).get("owner") or "the owner")
        step("task", "record:%s" % uuid, None, call["callback_at"],
             lambda: s.create_task(title, call["callback_at"], note_text(call, row), caller.get("user_uuid") or "", uuid))

    # 6. The note: for every outcome that says something, and for any call where the caller typed one.
    talked = int(call.get("seconds") or 0) >= NOTE_MIN_SECONDS
    if row.note == "always" or call.get("note") or talked:
        text = note_text(call, row)
        step("note", "record:%s" % uuid, None, text[:200], lambda: s.message(uuid, text))

    return {"mode": mode, "did": did, "already": skipped}


class RetryLater(Exception):
    def __init__(self, why: str, seconds: int):
        super().__init__(why)
        self.seconds = seconds


def store_job_payload(job_id: int, payload: dict) -> None:
    with store.tx() as c:
        c.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job_id))


# ---------------------------------------------------------------------- the other jobs
def run_kpi(call: dict) -> dict:
    import requests
    row = dispositions.TABLE.get(call.get("disposition") or "")
    caller = store.get_caller(call.get("identity") or "") or {}
    body = {
        "kind": "call", "id": call["id"], "tool": "siftstack-dialer",
        "at": call.get("started_at") or call.get("created_at"), "direction": call.get("direction"),
        "phone": call.get("number"), "line": call.get("from_number"), "seconds": int(call.get("seconds") or 0),
        "answered": bool(call.get("answered_at")), "answered_at": call.get("answered_at"),
        "outcome": row.label if row else (call.get("end_status") or ""), "status": call.get("end_status") or "",
        "record": call.get("record_uuid") or "", "owner": call.get("owner_uuid") or "",
        "person": caller.get("name") or call.get("identity") or "",
    }
    r = requests.post(config.S.kpi_url, json=body, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError("the KPI receiver answered %s" % r.status_code)
    return {"posted": True}


def run_slack(call: dict, kind: str) -> dict:
    import requests
    caller = (store.get_caller(call.get("identity") or "") or {}).get("name") or "A caller"
    try:
        detail = json.loads(call.get("detail") or "{}")
    except ValueError:
        detail = {}
    place = ", ".join(x for x in (detail.get("street"), detail.get("city")) if x) or "a record not in DataSift"
    link = detail.get("link") or ""
    if kind == "lead":
        text = "%s marked a lead on a call: %s. %s" % (caller, place, link)
    else:
        text = "Missed call with a voicemail for %s, from a number ending %s (%s). It is on the phone page." % (
            caller, phone.last4(call.get("number")), place)
    r = requests.post(config.S.slack_webhook, json={"text": text.strip()}, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError("Slack answered %s" % r.status_code)
    return {"posted": kind}


def run_fetch_vm(payload: dict) -> dict:
    import secrets as _s
    from . import twilio_voice
    ident = payload.get("identity") or ""
    caller = store.get_caller(ident)
    if not caller:
        return {"skipped": "no such caller"}
    audio = twilio_voice.voice().recording_bytes(payload["sid"], "mp3")
    folder = config.S.data_dir / "media"
    folder.mkdir(parents=True, exist_ok=True)
    name = "vm_%s_%d.mp3" % (ident, int(time.time()))
    (folder / name).write_bytes(audio)
    store.update_caller(ident, vm_file=name, vm_token=_s.token_urlsafe(18))
    from . import events
    events.publish(ident, "vm_saved", {"bytes": len(audio)})
    return {"saved": name, "bytes": len(audio)}


def run_auto_dispose(call: dict) -> dict:
    if call.get("disposition") or call.get("kind") in ("monitor", "vm_record"):
        return {"skipped": "already has an outcome"}
    from . import calls
    if call.get("status") not in ("ended", "failed"):
        return {"skipped": "still %s" % call.get("status")}
    pick = dispositions.suggest(call.get("end_status") or "", call.get("sip_code") or "", bool(call.get("answered_at")),
                                int(call.get("seconds") or 0), call.get("amd") or "", bool(call.get("vm_dropped")))
    if not pick or (call.get("answered_at") and not call.get("vm_dropped")):
        return {"left_for_a_person": True}
    res = calls.dispose(call["id"], call.get("identity") or "", pick, note="")
    return {"auto": pick, "ok": res.get("ok")}


# ---------------------------------------------------------------------- the loop
def run_one(job: dict) -> None:
    if not store.claim_job(job["id"]):
        return
    call = store.get_call(job.get("call_id") or "") or {}
    payload = json.loads(job.get("payload") or "{}")
    kind = job["kind"]
    try:
        if kind == "writeback":
            result = run_writeback(job, call)
            store.update_call(call["id"], writeback_state="skipped" if result.get("skipped") else
                              ("dry" if result.get("mode") == "dry" else "done"))
        elif kind == "texting":
            from . import texting
            result = texting.call_outcome(call)
        elif kind == "kpi":
            result = run_kpi(call)
        elif kind == "slack":
            result = run_slack(call, payload.get("kind") or "lead")
        elif kind == "fetch_vm":
            result = run_fetch_vm(payload)
        elif kind == "auto_dispose":
            result = run_auto_dispose(call)
        elif kind == "reconcile":
            from . import calls
            calls.reconcile(call.get("id") or "")
            result = {"checked": True}
        else:
            result = {"skipped": "unknown job %s" % kind}
        store.finish_job(job["id"], "done", result)
    except RetryLater as wait:
        with store.tx() as c:
            c.execute("UPDATE jobs SET attempts=attempts-1 WHERE id=?", (job["id"],))
        store.finish_job(job["id"], "queued", error=str(wait), retry_in_s=wait.seconds)
    except Exception as exc:  # noqa: BLE001 - one job must never stop the worker
        attempts = int(job.get("attempts") or 0) + 1
        log.warning("job %s (%s) failed, attempt %s: %s", job["id"], kind, attempts, str(exc)[:200])
        if attempts >= MAX_ATTEMPTS:
            store.finish_job(job["id"], "failed", error=str(exc)[:500])
            if kind == "writeback" and call.get("id"):
                store.update_call(call["id"], writeback_state="failed")
        else:
            store.finish_job(job["id"], "queued", error=str(exc)[:500], retry_in_s=min(30 * 2 ** attempts, 1800))


def drain(limit: int = 10) -> int:
    jobs = store.due_jobs(limit)
    for j in jobs:
        run_one(j)
    return len(jobs)


_STOP = threading.Event()


def sweep() -> None:
    """Once a minute: free stale queue claims and ask Twilio about calls whose last callback never came."""
    store.release_stale_items(30)
    for c in store.stale_calls(5):
        store.add_job(c["id"], "reconcile", {})


def run_forever(interval: float = 2.0) -> None:
    store.requeue_running()
    last_sweep = 0.0
    while not _STOP.is_set():
        try:
            drain()
            if time.time() - last_sweep > 60:
                sweep()
                last_sweep = time.time()
                store.kv_set("worker:alive", store.now())
        except Exception:  # noqa: BLE001
            log.exception("write-back pass failed")
        _STOP.wait(interval)


def start_thread() -> threading.Thread:
    t = threading.Thread(target=run_forever, daemon=True, name="dialer-writeback")
    t.start()
    return t


def stop() -> None:
    _STOP.set()
