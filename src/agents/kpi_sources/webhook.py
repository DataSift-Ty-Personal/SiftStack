"""Webhooks as a KPI source: one receiver for every tool that posts, mapped per tool.

The route (brain.py): POST /hooks/kpi/<AGENTS_KPI_HOOK_SECRET>/<connection>. It answers in milliseconds and does
nothing slow, the rule the texting app's receiver lives by (src/sms_agent/receiver.py):
  * the secret is checked the way the Slack relay checks its own: unset or wrong is a 404, so to a stranger the
    route does not exist, and so is a connection that is unknown, switched off or not a webhook;
  * when the connection names a signature (a header, an HMAC and the name of the secret it is keyed with), a
    delivery that does not carry it is refused before anything is kept;
  * THE RAW DELIVERY IS KEPT and the answer is 200. Reading it into events waits for the next count (collect()
    calls parse_pending), so a slow or failing parser never makes a tool retry, and a delivery is never lost to a
    wrong mapping: the raw stays AGENTS_KPI_RAW_DAYS (14) and a fixed mapping reads it again.
A retry is dropped on the delivery's own content (a byte-identical body is one delivery). A call's lifecycle (Kixie
posts Start Call, Answered Call and End Call) shares the call id, so the later deliveries fill the same act.
Message bodies are taken out before a delivery is kept: the count needs who, when and which way, never the words.

MAPPINGS turn a tool's payload into events as data, not code, so a tool whose field names move is fixed by editing
a mapping (or a connection's own mapping in its settings), never a parser. Each field takes several candidate
names, first found wins, and case does not matter. Two ship:
  * kixie: the call and SMS webhooks, from the field list Kixie publishes (call id, both numbers, direction, start
    and answer times, duration, status, disposition, the agent's email; message id, direction, date). NOT YET
    CHECKED AGAINST A REAL DELIVERY (verified is empty): read the first live delivery in kpisrc_raw before
    trusting a count from it. Kixie documents no signature, so the secret path is the only lock, as with smrtPhone.
  * zapier: our own field names, flat, for any Zapier trigger pointed at the route with Webhooks by Zapier
    (BatchDialer, BatchLeads, FreedomSoft, Mojo, InvestorFuse: the long tail, with no adapter code). The Zap names
    the kind: call, text, phone_status, mail, spend, or a stage word (lead, appointment, offer, contract, closed).
    A Zap that names a conversation is refused: conversations are counted from calls and texts by the catalog.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import parse_qs

from .. import store
from ..autopilot.gate import digits
from . import (ID_RX, KINDS, STAGES, TZ, Adapter, Batch, Event, Provides, answered_of, connection, direction_of,
               ensure, fmt_when, label, money_of, parse_when, save_events, seconds_of, setting, stored_events, words)

SECRET_ENV = "AGENTS_KPI_HOOK_SECRET"
MAX_BYTES = 256 * 1024
KEEP_HEADERS = ("content-type", "user-agent")
# Keys whose values are the words of a message or a call: never kept, never needed to count.
REDACT = {"message", "text", "body", "messagebody", "message_body", "content", "transcript", "transcription",
          "summary", "notes", "note"}
FIELDS = ("tool_id", "at", "direction", "phone", "line", "from", "to", "seconds", "answered", "answered_at",
          "call_status", "outcome", "status", "stage", "amount", "record", "owner", "campaign", "person", "tool")
KIND_WORDS = {"call": ("call", ""), "dial": ("call", ""), "text": ("text", ""), "sms": ("text", ""),
              "message": ("text", ""), "phone status": ("phone_status", ""), "phone_status": ("phone_status", ""),
              "number status": ("phone_status", ""), "mail": ("mail", ""), "letter": ("mail", ""),
              "spend": ("spend", ""), "charge": ("spend", ""), "lead": ("stage", "lead"),
              "new lead": ("stage", "lead"), "appointment": ("stage", "appointment"),
              "appt": ("stage", "appointment"), "offer": ("stage", "offer"), "contract": ("stage", "contract"),
              "under contract": ("stage", "contract"), "closed": ("stage", "closed"), "deal closed": ("stage", "closed")}
NO_CONVERSATION = ("a conversation is counted from calls and texts by the catalog's definition, never taken from a "
                   "tool: send the call or the text instead")

KIXIE = {
    "name": "Kixie",
    "tool": "kixie",
    "tz": "UTC",
    "verified": "",             # the day a real delivery was read against this mapping; empty until then
    "shapes": [
        {"kind": "call",
         "when": ["data.callid", "callid", "data.call_id", "call_id"],
         "fields": {
             "tool_id": ["data.callid", "callid", "data.call_id", "call_id"],
             "at": ["data.calldate", "calldate", "data.startdate", "startdate", "data.starttime", "starttime",
                    "data.date", "date", "data.createdate", "createdate", "timestamp"],
             "direction": ["data.calltype", "calltype", "data.direction", "direction", "data.type"],
             "from": ["data.fromnumber", "fromnumber", "data.from", "from", "data.callerid"],
             "to": ["data.tonumber", "tonumber", "data.to", "to", "data.customernumber"],
             "answered_at": ["data.answerdate", "answerdate", "data.answertime", "answertime"],
             "call_status": ["data.callstatus", "callstatus", "data.status", "status"],
             "seconds": ["data.duration", "duration", "data.talktime", "talktime", "data.callduration",
                         "callduration"],
             "outcome": ["data.disposition", "disposition", "data.outcome", "outcome", "data.callresult"],
             "person": ["data.email", "email", "data.agentemail", "agentemail", "data.useremail", "useremail",
                        "data.agent.email", "data.user.email"],
         }},
        {"kind": "text",
         "when": ["data.messageid", "messageid", "data.smsid", "smsid", "data.message", "message", "data.text"],
         "fields": {
             "tool_id": ["data.messageid", "messageid", "data.smsid", "smsid", "data.id", "id"],
             "at": ["data.date", "date", "data.createdate", "createdate", "data.timestamp", "timestamp"],
             "direction": ["data.direction", "direction", "data.type", "type"],
             "from": ["data.from", "from", "data.fromnumber", "fromnumber"],
             "to": ["data.to", "to", "data.tonumber", "tonumber"],
             "status": ["data.status", "status", "data.deliverystatus", "deliverystatus"],
             "person": ["data.email", "email", "data.agentemail", "agentemail", "data.useremail", "useremail"],
         }},
    ],
}

ZAPIER = {
    "name": "Zapier",
    "tool": "",
    "tz": "UTC",
    "verified": "ours",          # our own field names: a Zap is set up to send exactly these
    "shapes": [
        {"kind_from": ["kind", "type", "event", "event_type"],
         "when": ["kind", "type", "event", "event_type"],
         "fields": {
             "tool_id": ["id", "tool_id", "event_id", "call_id", "message_id"],
             "at": ["at", "time", "timestamp", "date", "created_at", "occurred_at"],
             "direction": ["direction"],
             "phone": ["phone", "number", "phone_number", "contact_phone"],
             "line": ["line", "our_number", "caller_id"],
             "from": ["from"],
             "to": ["to"],
             "seconds": ["seconds", "duration", "talk_time"],
             "answered": ["answered"],
             "outcome": ["outcome", "disposition", "result"],
             "status": ["status"],
             "stage": ["stage"],
             "amount": ["amount", "cost", "value"],
             "record": ["record", "record_id", "datasift_id"],
             "owner": ["owner", "owner_id"],
             "campaign": ["campaign", "list"],
             "person": ["person", "agent", "agent_email", "user", "email", "rep"],
             "tool": ["tool", "app"],
         }},
    ],
}
MAPPINGS = {"kixie": KIXIE, "zapier": ZAPIER}


# ---------------------------------------------------------------------- the mapping engine
def _get(obj, path: str):
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
                continue
            low = part.lower()
            hit = next((k for k in cur if str(k).lower() == low), None)
            if hit is None:
                return None
            cur = cur[hit]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def _first(obj, paths):
    for p in paths or ():
        v = _get(obj, p)
        if v not in (None, "", [], {}):
            return v
    return None


def mapping_of(conn: dict) -> tuple[dict | None, str]:
    """(the connection's mapping, "") or (None, why not)."""
    m = (conn.get("settings") or {}).get("mapping")
    if isinstance(m, dict):
        bad = mapping_problems(m)
        return (None, bad[0]) if bad else (m, "")
    if str(m or "") in MAPPINGS:
        return MAPPINGS[str(m)], ""
    return None, "the connection names no mapping (%s, or one of its own)" % ", ".join(MAPPINGS)


def mapping_problems(m: dict) -> list[str]:
    out = []
    shapes = m.get("shapes") if isinstance(m, dict) else None
    if not isinstance(shapes, list) or not shapes:
        return ["a mapping needs shapes: each with a kind (or kind_from), when, and fields"]
    for i, sh in enumerate(shapes, 1):
        if not isinstance(sh, dict):
            out.append("shape %d is not a mapping" % i)
            continue
        kind = str(sh.get("kind") or "")
        if "convers" in kind.lower():
            out.append("shape %d: %s" % (i, NO_CONVERSATION))
        elif kind and kind not in KINDS:
            out.append("shape %d: kind %r is not one of %s" % (i, kind, ", ".join(KINDS)))
        if not kind and not sh.get("kind_from"):
            out.append("shape %d names neither a kind nor where to read one (kind_from)" % i)
        for f, paths in (sh.get("fields") or {}).items():
            if f not in FIELDS:
                out.append("shape %d: field %r is not one of %s" % (i, f, ", ".join(FIELDS)))
            elif not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                out.append("shape %d: field %s takes a list of paths" % (i, f))
    return out


def map_payload(mapping: dict, payload: dict, received: datetime, conn: dict, raw_ref: str = "") -> tuple[list, list]:
    """(events, problems) for one delivery. The first shape whose `when` path is present reads it."""
    tz = str((conn.get("settings") or {}).get("tz") or mapping.get("tz") or setting("AGENTS_KPI_TZ", TZ))
    for shape in mapping.get("shapes") or []:
        if shape.get("when") and _first(payload, shape["when"]) is None:
            continue
        f = shape.get("fields") or {}

        def g(name, f=f):
            return _first(payload, f.get(name) or ())

        kind, stage = str(shape.get("kind") or ""), ""
        if not kind:
            word = words(_first(payload, shape.get("kind_from") or ()))
            if "convers" in word:
                return [], [NO_CONVERSATION]
            kind, stage = KIND_WORDS.get(word, ("", ""))
            if not kind:
                return [], ["the delivery names a kind I do not know (%s)" % (word[:30] or "blank")]
        if kind == "stage":
            stage = KIND_WORDS.get(words(g("stage")), ("", ""))[1] or (words(g("stage")) if words(g("stage")) in STAGES
                                                                        else stage)
        direction = direction_of(g("direction"))
        frm, to = digits(g("from")), digits(g("to"))
        phone = digits(g("phone")) or (to if direction == "out" else frm if direction == "in" else "")
        line = digits(g("line")) or (frm if direction == "out" else to if direction == "in" else "")
        at, exact = parse_when(g("at"), tz)
        if at is None:
            at, exact = received, True
        answered = None
        if kind == "call":
            answered = answered_of(g("answered"))
            if answered is None and g("answered_at") is not None:
                answered = True
            if answered is None:
                answered = answered_of(g("call_status"))
        tool_id = str(g("tool_id") or "").strip()
        sid = ("id:%s" % tool_id) if tool_id else "body:%s" % hashlib.sha1(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]
        try:
            ev = Event(kind=kind, at=at, exact=exact, source=conn["id"], source_id=sid,
                       tool=g("tool") or conn.get("tool") or mapping.get("tool") or "", tool_id=tool_id,
                       person_ref=str(g("person") or ""), direction=direction, phone=phone, line=line,
                       record=str(g("record") or ""), owner=str(g("owner") or ""), answered=answered,
                       seconds=seconds_of(g("seconds")), outcome=str(g("outcome") or ""), stage=stage,
                       status=str(g("status") or ""), amount=money_of(g("amount")), campaign=str(g("campaign") or ""),
                       raw_ref=raw_ref)
        except ValueError as exc:
            return [], [str(exc)[:160]]
        return [ev], []
    return [], ["the delivery matched none of the %s shapes" % (mapping.get("name") or "mapping's")]


# ---------------------------------------------------------------------- the receiver
def secret_ok(secret: str) -> bool:
    """The relay's rule: no secret set, no route; a wrong one is a 404, never a hint."""
    want = (os.environ.get(SECRET_ENV) or "").strip()
    return bool(want) and hmac.compare_digest(str(secret or ""), want)


def signature_ok(sig: dict, headers: dict, raw: bytes) -> tuple[bool, str]:
    """An HMAC of the raw body, keyed with the secret the setting names (never the secret itself), compared in hex
    or base64, after an optional prefix such as "sha256="."""
    env = str(sig.get("secret_env") or "")
    secret = (os.environ.get(env) or "").strip()
    if not secret:
        return False, "the signing secret %s is not set" % (env or "(unnamed)")
    want = str(sig.get("header") or "").lower()
    got = next((str(v) for k, v in (headers or {}).items() if str(k).lower() == want), "").strip()
    if not got:
        return False, "the delivery carries no %s header" % sig.get("header")
    prefix = str(sig.get("prefix") or "")
    if prefix and got.startswith(prefix):
        got = got[len(prefix):]
    algo = hashlib.sha1 if str(sig.get("algo") or "sha256").lower() == "sha1" else hashlib.sha256
    mac = hmac.new(secret.encode("utf-8"), raw or b"", algo).digest()
    if hmac.compare_digest(got, mac.hex()) or hmac.compare_digest(got, base64.b64encode(mac).decode("ascii")):
        return True, ""
    return False, "the signature does not match"


def parse_body(raw: bytes, content_type: str = "") -> list[dict] | None:
    """The deliveries in a body: a JSON object, a JSON list of objects, or a form (Zapier can send either)."""
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if data is None and ("=" in text and "{" not in text[:1]):
        form = {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items() if v}
        if "payload" in form:
            try:
                data = json.loads(form["payload"])
            except ValueError:
                data = form
        else:
            data = form or None
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
        return data
    return None


def _redact(v):
    if isinstance(v, dict):
        return {k: ("[removed]" if str(k).lower() in REDACT and v[k] not in (None, "") else _redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [_redact(x) for x in v]
    return v


def receive(secret: str, source: str, headers: dict, raw: bytes, now: datetime | None = None) -> tuple[int, dict]:
    """(status code, body) for one delivery. Keeps it and answers; reading it waits for the next count."""
    if not secret_ok(secret) or not ID_RX.match(str(source or "")):
        return 404, {}
    conn = connection(source)
    if not conn or not conn.get("enabled") or conn.get("adapter") != "webhook":
        return 404, {}
    if len(raw or b"") > max(1024, setting("AGENTS_KPI_HOOK_MAX_BYTES", MAX_BYTES)):
        return 413, {"ok": False, "why": "the delivery is too large"}
    if not (raw or b"").strip():
        return 400, {"ok": False, "why": "the delivery is empty"}
    sig = (conn.get("settings") or {}).get("signature")
    if isinstance(sig, dict) and sig:
        ok, why = signature_ok(sig, headers, raw)
        if not ok:
            return 401, {"ok": False, "why": why}
    digest = hashlib.sha256(raw).hexdigest()
    ctype = next((str(v) for k, v in (headers or {}).items() if str(k).lower() == "content-type"), "")
    payloads = parse_body(raw, ctype)
    body = json.dumps(_redact(payloads if len(payloads or []) != 1 else payloads[0]), default=str) if payloads \
        else (raw or b"").decode("utf-8", "replace")[:20000]
    kept = {k.lower(): str(v)[:200] for k, v in (headers or {}).items() if str(k).lower() in KEEP_HEADERS}
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    ensure()
    with store._LOCK, store._db() as c:
        cur = c.execute("INSERT OR IGNORE INTO kpisrc_raw (at, source, digest, headers, body) VALUES (?,?,?,?,?)",
                        (stamp, source, digest, json.dumps(kept), body))
        if not cur.rowcount:
            return 200, {"ok": True, "duplicate": True}
    return 200, {"ok": True}


def parse_pending(conn: dict, limit: int | None = None) -> tuple[int, list]:
    """Read the deliveries kept since the last count into events. Returns (new acts, problems)."""
    ensure()
    mapping, why = mapping_of(conn)
    with store._LOCK, store._db() as c:
        rows = c.execute("SELECT id, at, body FROM kpisrc_raw WHERE source=? AND parsed='' ORDER BY id LIMIT ?",
                         (conn["id"], int(limit or setting("AGENTS_KPI_PARSE_BATCH", 5000)))).fetchall()
    if not rows:
        return 0, []
    if mapping is None:
        return 0, [why]
    new, marks = 0, []
    for r in rows:
        payloads = parse_body((r["body"] or "").encode("utf-8"))
        if payloads is None:
            marks.append((r["id"], "problem", "the body is not JSON or a form"))
            continue
        try:
            received = datetime.fromisoformat(r["at"])
        except ValueError:
            received = datetime.now(timezone.utc)
        events, problems = [], []
        for p in payloads:
            evs, probs = map_payload(mapping, p, received, conn, raw_ref="raw:%s" % r["id"])
            events += evs
            problems += probs
        new += save_events(events) if events else 0
        marks.append((r["id"], "problem" if problems and not events else "yes", "; ".join(problems)[:300]))
    with store._LOCK, store._db() as c:
        for rid, state, problem in marks:
            c.execute("UPDATE kpisrc_raw SET parsed=?, problem=? WHERE id=?", (state, problem, rid))
    return new, []


def reparse(source: str) -> int:
    """Read the deliveries still kept for one connection again, after its mapping was fixed. What they gave before is
    dropped first (an event remembers the delivery it came from), so a wrong reading is replaced, not added to. An
    act older than the kept deliveries is left as it is. Returns how many deliveries will be read on the next count."""
    ensure()
    with store._LOCK, store._db() as c:
        ids = {"raw:%s" % r["id"] for r in c.execute("SELECT id FROM kpisrc_raw WHERE source=?", (source,)).fetchall()}
    gone = [e.source_id for e in stored_events(source) if e.raw_ref in ids]
    with store._LOCK, store._db() as c:
        for sid in gone:
            c.execute("DELETE FROM kpisrc_event WHERE source=? AND source_id=?", (source, sid))
        c.execute("UPDATE kpisrc_raw SET parsed='', problem='' WHERE source=?", (source,))
    return len(ids)


class Webhook(Adapter):
    id = "webhook"
    tool = ""
    provides = tuple(Provides(k, "webhook", frozenset({"tool_id", "person_ref", "direction", "phone", "line", "record",
                                                        "owner", "answered", "seconds", "outcome", "status", "stage",
                                                        "amount", "campaign"}), per_person=True, scope="all",
                              latency="push", backfill_days=0) for k in KINDS)
    normal_gap_hours = 6.0

    def kinds(self, conn=None) -> set:
        mapping, _why = mapping_of(conn or {})
        if mapping is None:
            return set()
        out = set()
        for sh in mapping.get("shapes") or []:
            out |= {sh["kind"]} if sh.get("kind") in KINDS else set(KINDS)
        return out

    def settings_problems(self, settings: dict) -> list:
        s, out = settings or {}, []
        m = s.get("mapping")
        if isinstance(m, dict):
            out += mapping_problems(m)
        elif str(m or "") not in MAPPINGS:
            out.append("mapping is %s, or a mapping of its own" % " or ".join(MAPPINGS))
        sig = s.get("signature")
        if sig is not None:
            if not isinstance(sig, dict) or not sig.get("header") or not sig.get("secret_env"):
                out.append("signature needs the header the tool signs in and secret_env, the name of the setting that "
                           "holds the signing secret (never the secret itself)")
            elif str(sig.get("algo") or "sha256").lower() not in ("sha256", "sha1"):
                out.append("signature algo is sha256 or sha1")
            elif any(k in sig for k in ("secret", "key", "value")):
                out.append("signature names the setting that holds the secret (secret_env), never the secret itself")
        return out

    def pull(self, conn, since, until, kinds, now=None) -> Batch:
        b = Batch()
        _new, problems = parse_pending(conn)
        b.problems += problems
        b.events = [e for e in stored_events(conn["id"], since, until) if e.kind in kinds]
        with store._LOCK, store._db() as c:
            last = c.execute("SELECT MAX(at) AS at FROM kpisrc_raw WHERE source=?", (conn["id"],)).fetchone()
            bad = c.execute("SELECT COUNT(*) AS n, MIN(at) AS first, MIN(problem) AS said FROM kpisrc_raw "
                            "WHERE source=? AND parsed='problem'", (conn["id"],)).fetchone()
        if last and last["at"]:
            b.last_at = datetime.fromisoformat(last["at"])
        elif b.events:
            b.last_at = max(e.at for e in b.events)
        if bad and bad["n"]:
            first = datetime.fromisoformat(bad["first"]) if bad["first"] else None
            b.problems.append("%s: some deliveries could not be read (%d kept since %s; one said: %s)" % (
                label(conn), bad["n"], fmt_when(first) if first else "earlier", (bad["said"] or "nothing")[:120]))
        return b


ADAPTER = Webhook()
