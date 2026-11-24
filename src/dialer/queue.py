"""The single-line power dialer: one DataSift preset, walked one record at a time.

A caller picks a call preset (the ones they already work: FTM - 02 Ready to Call, Estate - 02, Top 1000 -
Ready to Call, their own queue in CALLER QUEUES). The session reads the preset's records in the order
DataSift returns them, 250 at a time, and hands the caller one record at a time:

  * every record passes the gate before the caller sees it (Do Not Market, a litigator, an opt-out, a death
    on the record, a sale, mail only, the owner in a higher lane: skipped, with the reason kept);
  * a preset that asks for a status works that status (a lead follow-up queue calls leads, a second-chance
    queue calls Not Interested); the rules about the person are never lifted this way;
  * only callable numbers are dialed (a top tier or a number a person confirmed), a confirmed number first,
    then by tier, each number once a day, and only inside the hours where that number is;
  * a record another caller has open, or whose owner another caller is talking to, is passed over and left
    for later; a record claimed and abandoned for 30 minutes goes back in the pile.

One line, one call at a time, with the caller on the line before the homeowner is dialed. That is what
keeps it clear of the abandoned-call rules a multi-line predictive dialer lives under.

After each call the disposition decides what is next: someone who owns the house was reached (or family)
and the record is done; no answer, a voicemail, a wrong or dead number and the next number on the record
is dialed; when the numbers run out the record is done.
"""
from __future__ import annotations

import json
import logging
import threading

from . import config, rules, sift, store

log = logging.getLogger(__name__)

PAGE = 250
MAX_ROWS = 10000          # the records search refuses offset + limit past 10,000
READS_PER_ASK = 25        # records read and judged in one "next" before the page is told to ask again
DONE_AFTER = ("not_interested", "listed", "call_back", "interested", "do_not_contact", "relative", "owner_deceased")


def presets() -> list[dict]:
    """The presets a caller may dial from: any whose folder or title carries one of DIALER_PRESET_WORDS."""
    words = config.S.preset_words
    out = []
    for title, p in sift.client().presets().items():
        hay = ("%s %s" % (p.get("folder") or "", title)).upper()
        if not words or any(w in hay for w in words):
            out.append({"title": title, "folder": p.get("folder") or ""})
    out.sort(key=lambda r: (r["folder"], r["title"]))
    return out


def start(identity: str, title: str) -> dict:
    if title not in {p["title"] for p in presets()}:
        return {"ok": False, "error": "that preset is not one the dialer offers"}
    s = sift.client()
    must, dropped = s.preset_must(title)
    total, rows = s.search(must, limit=PAGE, offset=0, interactive=True)
    allow = rules.allow_for_preset(must)
    old = store.open_session_for(identity)
    if old:
        store.update_session(old["id"], state="done")
    sid = store.new_session(identity, title, s.presets()[title]["uuid"], allow, total)
    store.add_items(sid, rows)
    store.update_session(sid, loaded=len(rows))
    out = {"ok": True, "session": describe(sid)}
    if dropped:
        out["note"] = "part of this preset's filter could not be read and was left out: %s" % "; ".join(dropped[:3])
    return out


def _load_more(session: dict) -> int:
    loaded = int(session.get("loaded") or 0)
    total = int(session.get("total") or 0)
    if loaded >= min(total, MAX_ROWS):
        return 0
    must, _ = sift.client().preset_must(session["preset"])
    _, rows = sift.client().search(must, limit=min(PAGE, MAX_ROWS - loaded), offset=loaded, interactive=True)
    added = store.add_items(session["id"], rows)
    store.update_session(session["id"], loaded=loaded + len(rows))
    return len(rows) if rows else 0


def describe(session_id: int) -> dict:
    s = store.get_session(session_id) or {}
    counts = store.session_counts(session_id)
    return {"id": session_id, "preset": s.get("preset"), "state": s.get("state"), "total": s.get("total"),
            "loaded": s.get("loaded"), "counts": counts, "skipped_for": store.skip_reasons(session_id)}


def _busy() -> set:
    return {c.get("record_uuid") for c in store.active_calls() if c.get("record_uuid")}


def _owner_busy(owner_uuid: str, identity: str) -> bool:
    return any(c.get("owner_uuid") == owner_uuid and c.get("identity") != identity for c in store.active_calls())


def next_item(session_id: int, identity: str) -> dict:
    """The next record to call, its numbers in dialing order, and the record as the caller sees it."""
    session = store.get_session(session_id)
    if not session or session.get("identity") != identity:
        return {"ok": False, "error": "that is not your session"}
    if session["state"] == "done":
        return {"ok": True, "done": True, "session": describe(session_id)}
    # A record the caller has claimed but not finished comes back first (the page reloaded mid-record).
    for r in store.conn().execute("SELECT * FROM queue_items WHERE session_id=? AND state='claimed' AND claimed_by=?"
                                  " ORDER BY seq LIMIT 1", (session_id, identity)):
        item = dict(r)
        if item.get("numbers"):
            return _present(item)
    skipped = 0
    while skipped < READS_PER_ASK:
        item = store.claim_next_item(session_id, identity, busy_records=_busy())
        if item is None:
            if _load_more(store.get_session(session_id)):
                continue
            store.update_session(session_id, state="done")
            return {"ok": True, "done": True, "session": describe(session_id)}
        row = json.loads(item.get("row") or "{}")
        try:
            rec = sift.client().record(item["record_uuid"])
        except Exception as exc:  # noqa: BLE001 - one unreadable record does not stop the session
            store.update_item(item["id"], state="pending", claimed_by=None, claimed_at=None)
            return {"ok": False, "error": "DataSift could not be read (%s); try again in a moment" % str(exc)[:80]}
        allow = json.loads(session.get("allow") or "[]")
        ok, why, numbers = rules.queue(rec, row, allow)
        owner_uuid = (rec.get("owner") or {}).get("uuid") or ""
        if ok and owner_uuid and _owner_busy(owner_uuid, identity):
            store.update_item(item["id"], state="pending", claimed_by=None, claimed_at=None)
            skipped += 1
            continue
        if not ok:
            store.update_item(item["id"], state="skipped", reason=why, done_at=store.now())
            skipped += 1
            continue
        store.update_item(item["id"], numbers=numbers, owner_uuid=owner_uuid)
        item = store.get_item(item["id"])
        _prefetch(session_id)
        return _present(item, rec, row)
    return {"ok": True, "more": True, "skipped": skipped, "session": describe(session_id)}


def _present(item: dict, rec: dict | None = None, row: dict | None = None) -> dict:
    rec = rec or sift.client().record(item["record_uuid"])
    row = row if row is not None else json.loads(item.get("row") or "{}")
    numbers = json.loads(item.get("numbers") or "[]") if isinstance(item.get("numbers"), str) else (item.get("numbers") or [])
    i = int(item.get("next_number") or 0)
    return {"ok": True, "item": {"id": item["id"], "session_id": item["session_id"], "record_uuid": item["record_uuid"],
                                 "numbers": numbers, "next": i, "number": numbers[i]["number"] if i < len(numbers) else ""},
            "record": sift.summary(rec, row), "session": describe(item["session_id"])}


def _prefetch(session_id: int) -> None:
    """Warm the next pending record so the next "next" answers at once."""
    r = store.conn().execute("SELECT record_uuid FROM queue_items WHERE session_id=? AND state='pending' ORDER BY seq LIMIT 1",
                             (session_id,)).fetchone()
    if not r:
        return
    uid = r[0]

    def warm():
        try:
            sift.client().record(uid, interactive=False)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=warm, daemon=True).start()


def mark_dialed(call: dict) -> None:
    """Nothing to change on the item when a number is dialed; the disposition moves it. Kept as the hook."""
    return None


def after_call(call: dict) -> dict:
    """Move the record on after a disposition: done, or the next number."""
    item = store.get_item(int(call.get("item_id") or 0))
    if not item:
        return {}
    numbers = json.loads(item.get("numbers") or "[]")
    key = call.get("disposition") or ""
    nxt = int(item.get("next_number") or 0) + 1
    if key in DONE_AFTER or nxt >= len(numbers):
        store.update_item(item["id"], state="done", next_number=nxt, done_at=store.now(), reason=key)
        return {"record_done": True}
    store.update_item(item["id"], next_number=nxt)
    return {"record_done": False, "number": numbers[nxt]["number"]}


def skip(item_id: int, identity: str, reason: str = "skipped by the caller") -> dict:
    item = store.get_item(item_id)
    if not item or item.get("claimed_by") != identity:
        return {"ok": False, "error": "that record is not yours"}
    store.update_item(item_id, state="skipped", reason=reason[:120], done_at=store.now())
    return {"ok": True}


def stop(session_id: int, identity: str) -> dict:
    s = store.get_session(session_id)
    if not s or s.get("identity") != identity:
        return {"ok": False, "error": "that is not your session"}
    store.update_session(session_id, state="done")
    with store.tx() as c:
        c.execute("UPDATE queue_items SET state='pending', claimed_by=NULL, claimed_at=NULL"
                  " WHERE session_id=? AND state='claimed'", (session_id,))
    return {"ok": True, "session": describe(session_id)}
