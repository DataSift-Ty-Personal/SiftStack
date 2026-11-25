"""
pull_kpis.py - self-contained DataSift (REISift) KPI puller for the kpi-engine skill.

Reads YOUR account's activity log on your DataSift Open API key and produces a
graded KPI report: dials, answer/conversation/contact rates, correct numbers,
dispositions, leads (including new_lead statuses), talk time, per caller and
per day, with funnel pacing. Markdown + CSV always; Excel with --xlsx (needs
openpyxl); optional Slack digest with --slack <webhook-url>.

YOUR KEY (never expires):
  An account admin creates the Open API key in DataSift (developers.datasift.ai
  explains it). Give it to the script one of two ways:
    set REISIFT_API_KEY=<your key>          (environment variable), or
    a .env file with REISIFT_API_KEY=<your key> in the folder you run from,
    or next to this script.
  It is sent as the header "Authorization: Api-Key <key>".

HOW IT READS
  One pass over the account activity log for the period (calls, texts, phone
  statuses, record statuses, completed tasks), 100 events a request. --detail
  adds a few record reads to put an address beside each owner worked. Every
  request is a GET: the script never changes anything in your account.
  DataSift allows 120 requests a minute per account, shared by every tool on
  it; this script paces itself at about 80 and waits when asked to.

USAGE
  python pull_kpis.py --days 7
  python pull_kpis.py --from 2026-07-06 --to 2026-07-16 --xlsx
  python pull_kpis.py --days 1 --slack https://hooks.slack.com/services/...
  python pull_kpis.py --show-benchmarks
  python pull_kpis.py --days 7 --tz America/Chicago
  python pull_kpis.py --selftest          (checks the counting rules, no network)

Benchmarks: defaults below; override any key in a benchmarks.json next to this
script (add your admin users to excluded_callers, custom lead statuses, etc.).
Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
API = "https://apiv2.reisift.io"
UTC = ZoneInfo("UTC")

DEFAULT_BENCHMARKS = {
    "dials_floor_per_caller": 150,
    "dials_target_per_caller": 200,
    "conversations_floor_per_caller": 5,
    "conversation_min_seconds": 60,
    "meaningful_conversation_min_seconds": 120,
    "voicemail_max_seconds": 30,
    "dials_per_correct_scored": 9,
    "dials_per_correct_blind": 32,
    "correct_numbers_per_deal": 100,
    "leads_per_caller_day": [2, 3],
    "leads_per_contract": [15, 20],
    "appointment_take_rate": 0.25,
    "lead_statuses": ["Cold Lead", "Warm Lead", "Hot Lead", "new_lead", "New Lead",
                      "No Contact New Lead", "Nurture New Lead"],
    "excluded_callers": [],
}

CORRECT_STATES = {"CORRECT", "CORRECT_DNC"}
WRONG_STATES = {"WRONG", "WRONG_DNC"}
MADE, ANSWERED, NOANSWER = "owner.call.made", "owner.call.answered", "owner.call.noanswer"
RECEIVED, MISSED = "owner.call.received", "owner.call.missed"
CALL_EVENTS = {MADE, ANSWERED, NOANSWER, RECEIVED, MISSED}
SMS_SENT, SMS_RECEIVED = "owner.sms.sent", "owner.sms.received"
PHONE_STATUS, PROPERTY_STATUS, TASK_DONE = ("owner.phone.status.updated", "property.status.updated",
                                            "task.completed")
LOG_TYPES = sorted(CALL_EVENTS | {SMS_SENT, SMS_RECEIVED, PHONE_STATUS, PROPERTY_STATUS, TASK_DONE})
UNATTRIBUTED = "unattributed"
ANSWER_JOIN = datetime.timedelta(hours=2)

LOG_PAGE = 100      # the activity log refuses a page over 100
SEARCH_PAGE = 250
SEARCH_BATCH = 50   # ids per records query, keeps the URL short
GAP = 0.75          # seconds between requests: about 80 a minute


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_benchmarks() -> dict:
    bench = dict(DEFAULT_BENCHMARKS)
    f = HERE / "benchmarks.json"
    if f.exists():
        bench.update(json.loads(f.read_text(encoding="utf-8")))
    return bench


# ---- the key and the requests ----
def api_key() -> str:
    key = os.environ.get("REISIFT_API_KEY", "").strip()
    if not key:
        for f in (Path.cwd() / ".env", HERE / ".env"):
            if not f.is_file():
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                k, _, v = line.partition("=")
                if k.strip() == "REISIFT_API_KEY" and v.strip():
                    key = v.strip().strip('"').strip("'")
            if key:
                break
    if not key:
        sys.exit("No API key. Set REISIFT_API_KEY to your DataSift Open API key "
                 "(see the top of this script).")
    return key


_last_call = [0.0]


def get(key: str, path: str, params: dict | None = None, tries: int = 6):
    """One GET on the Open API key, paced, with the 429 hint and a short retry on a gateway error."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(tries):
        wait = GAP - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()
        r = urllib.request.Request(url, headers={"Authorization": f"Api-Key {key}",
                                                 "Accept": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body = (e.read() or b"").decode("utf-8", "replace")[:300]
            if e.code == 401:
                sys.exit("The API key was refused (HTTP 401). Check REISIFT_API_KEY: it must be your "
                         "DataSift Open API key.")
            if e.code == 429 and attempt < tries - 1:
                m = re.search(r"available in (\d+)", body, re.I)
                hint = e.headers.get("Retry-After") if e.headers else None
                wait_s = int(m.group(1)) + 1 if m else (int(hint) if hint and hint.isdigit() else 2 ** attempt)
                log(f"rate limited, waiting {min(wait_s, 90)}s")
                time.sleep(min(wait_s, 90))
                continue
            if e.code in (502, 503, 504) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"network error on {path}: {e}") from None
    raise RuntimeError(f"gave up on {path} after {tries} tries")


def zulu(dt: datetime.datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_log(key: str, start: datetime.datetime, end: datetime.datetime) -> list[dict]:
    """Every KPI event in [start, end), newest first, from the account activity log. Paging is by
    cursor. Only any_event_type filters the log; a guessed filter name is ignored, never an error."""
    must = {"timestamp": [zulu(start), zulu(end)], "any_event_type": LOG_TYPES}
    out, cursor, pages = [], "", 0
    while True:
        params = {"query": json.dumps({"must": must}), "limit": LOG_PAGE}
        if cursor:
            params["cursor"] = cursor
        r = get(key, "/api/internal/property/logs/", params)
        rows = r.get("results") or []
        out.extend(rows)
        pages += 1
        if pages % 25 == 0:
            log(f"  {len(out)} events read...")
        cursor = r.get("next_cursor") or ""
        if not cursor or not rows:
            break
    return out


def records_by(key: str, field: str, ids) -> list[dict]:
    """Records search rows for a list of owner ids (field "owners") or record ids ("properties"), by GET."""
    ids = sorted({i for i in ids if i})
    out = []
    for i in range(0, len(ids), SEARCH_BATCH):
        chunk, offset = ids[i:i + SEARCH_BATCH], 0
        while True:
            r = get(key, "/api/internal/property/", {"query": json.dumps({"must": {field: chunk}}),
                                                     "limit": SEARCH_PAGE, "offset": offset})
            rows = r.get("results") or r.get("data") or []
            out.extend(rows)
            offset += len(rows)
            if not rows or offset >= int(r.get("count") or 0) or offset >= 10000:
                break
    return out


# ---- event helpers ----
def local_dt(ts: str, tz):
    try:
        return (datetime.datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=UTC).astimezone(tz))
    except Exception:
        return None


def digits(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    return d[-10:] if len(d) >= 10 else d


def person_of(obj) -> tuple[str, str]:
    """(email, display name) of the person on a call, text or log row; ("", "") when it names nobody."""
    if not isinstance(obj, dict):
        return "", ""
    email = str(obj.get("email") or "").strip()
    name = str(obj.get("name") or "").strip() or \
        f"{obj.get('first_name') or ''} {obj.get('last_name') or ''}".strip()
    return (email, name or email) if "@" in email else ("", "")


def caller_on(call: dict) -> tuple[str, str]:
    """The caller a call event names. A dial event often names nobody (Kixie logs the caller only
    on the answer), so this is one of several sources, never the only one."""
    for obj in (call.get("external_user"), call.get("user")):
        who = person_of(obj)
        if who[0]:
            return who
    return "", ""


def author_of(ev: dict) -> tuple[str, str]:
    """The person who wrote a row, or ("", "") when a dialer, a sequence or the system wrote it."""
    who = person_of(ev.get("author_extra_info"))
    if who[0]:
        return who
    a = str(ev.get("author") or "")
    return (a, a) if "@" in a else ("", "")


def detect_new_index(events, key):
    """Status payloads carry [a,b] with no guaranteed order; vote by chaining."""
    by_tgt = defaultdict(list)
    for ev in events:
        obj = (ev.get("payload") or {}).get("owner" if key == "phone" else "property")
        if not isinstance(obj, dict):
            continue
        pair = obj.get("status")
        tgt = obj.get("phone") if key == "phone" else ev.get("resource")
        if isinstance(pair, list) and len(pair) == 2 and tgt:
            by_tgt[tgt].append((ev.get("timestamp", ""), pair))
    old_new = new_old = 0
    for seq in by_tgt.values():
        seq.sort()
        for (_, e1), (_, e2) in zip(seq, seq[1:]):
            if e1[1] == e2[0] and e1[1] != e2[1]:
                old_new += 1
            elif e1[0] == e2[1] and e1[0] != e2[0]:
                new_old += 1
    return 0 if new_old > old_new else 1


def status_pair(ev, key, idx):
    """(old, new) of a status change; old is None when only the new value was logged."""
    pair = ((ev.get("payload") or {}).get("owner" if key == "phone" else "property") or {}).get("status")
    if isinstance(pair, list) and len(pair) == 2:
        return pair[1 - idx], pair[idx]
    if isinstance(pair, list) and pair:
        return None, pair[-1]
    return None, pair


def blank():
    return {"dials": 0, "answered": 0, "noanswer": 0, "talk_seconds": 0, "sms_sent": 0,
            "sms_received": 0, "conversations": 0, "meaningful_conversations": 0,
            "band_vm": 0, "band_brief": 0, "correct_numbers": 0, "wrong_numbers": 0,
            "dead_numbers": 0, "dnc_numbers": 0, "leads": 0, "not_interested": 0,
            "follow_ups": 0, "appointments": 0, "inbound_answered": 0, "inbound_missed": 0,
            "records": set(), "days": set(), "first_call": None, "last_call": None}


# ---- counting (pure: no network, so --selftest can drive it) ----
def count(raw_events: list[dict], day_from: str, day_to: str, tz, bench: dict) -> dict:
    lead_set = {s.lower() for s in bench["lead_statuses"]}  # case-insensitive: DataSift mixes casings
    excluded = {e.lower() for e in bench["excluded_callers"]}
    conv_s, mean_s, vm_s = (bench["conversation_min_seconds"],
                            bench["meaningful_conversation_min_seconds"],
                            bench["voicemail_max_seconds"])

    seen_ids, events = set(), []
    for e in raw_events:
        eid = e.get("id")
        if eid is not None:
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
        dt = local_dt(e.get("timestamp", ""), tz)
        if dt and day_from <= dt.date().isoformat() <= day_to:
            events.append((dt, e))
    events.sort(key=lambda x: (x[0], str(x[1].get("id") or "")))

    acct, per, daily = blank(), defaultdict(blank), defaultdict(blank)
    names = {UNATTRIBUTED: "Unattributed"}

    def bump(email, day, field, amt=1):
        if email.lower() in excluded:
            return                      # admin and test users stay out of every table
        acct[field] += amt
        per[email][field] += amt
        daily[day][field] += amt

    # 1. Calls: one call however many rows describe it (the dial, then its answer or no answer).
    calls, by_id = [], {}
    open_dials = defaultdict(list)          # (owner, number) -> outbound calls not yet answered
    for dt, ev in events:
        et = ev.get("event_type")
        if et not in CALL_EVENTS:
            continue
        c = (ev.get("payload") or {}).get("call") or {}
        cid = c.get("uuid") or c.get("external_id")
        direction = str(c.get("direction") or "").lower()
        # The row's own direction decides: Kixie logs an outbound no-answer as "missed" too.
        inbound = direction.startswith("in") if direction else et in (RECEIVED, MISSED)
        number = digits(c.get("origin_number") if inbound else c.get("destination_number"))
        line = digits(c.get("destination_number") if inbound else c.get("origin_number"))
        owner = ev.get("resource") or c.get("owner") or ""
        rec = by_id.get(cid) if cid else None
        if rec is None and et in (ANSWERED, NOANSWER, MISSED) and not inbound:
            # An answer whose id matches no dial joins the latest open dial to the same owner and
            # number in the two hours before it, so one dial can never become two.
            cands = [x for x in open_dials[(owner, number)]
                     if not x["closed"] and dt - ANSWER_JOIN <= x["at"] <= dt]
            rec = max(cands, key=lambda x: x["at"]) if cands else None
        if rec is None:
            rec = {"at": dt, "inbound": inbound, "made": False, "answered": False, "noanswer": False,
                   "duration": 0, "caller": "", "owner": owner, "number": number, "line": line,
                   "closed": False}
            calls.append(rec)
            if cid:
                by_id[cid] = rec
        who = caller_on(c)
        if who[0] and not rec["caller"]:
            rec["caller"] = who[0]
            names.setdefault(who[0], who[1])
        if et == MADE and not inbound:
            rec["made"], rec["at"] = True, dt
            open_dials[(owner, number)].append(rec)
        elif et in (ANSWERED, RECEIVED):
            # Kixie logs an incoming call as "received" while it rings (no duration) and then
            # "missed" on the same call id, so a ring with no duration decides nothing yet.
            dur = int(c.get("duration") or 0)
            if et == ANSWERED or dur > 0:
                rec["answered"], rec["closed"] = True, True
                rec["duration"] = max(rec["duration"], dur)
        elif et in (NOANSWER, MISSED):
            # An outbound call nobody picked up can arrive as "missed" on the dial's own id.
            rec["noanswer"], rec["closed"] = True, True

    # A dial that names no caller goes to the one caller seen on the same line that day, else to
    # the one caller who worked that day, else it stays unattributed (still in the account totals).
    on_line, on_day = defaultdict(set), defaultdict(set)
    for rec in calls:
        if not rec["inbound"] and rec["caller"]:
            day = rec["at"].date().isoformat()
            on_line[(day, rec["line"])].add(rec["caller"])
            on_day[day].add(rec["caller"])
    for rec in calls:
        if rec["inbound"] or rec["caller"]:
            continue
        day = rec["at"].date().isoformat()
        line_callers, day_callers = on_line.get((day, rec["line"])) or set(), on_day.get(day) or set()
        if rec["line"] and len(line_callers) == 1:
            rec["caller"] = next(iter(line_callers))
        elif len(day_callers) == 1:
            rec["caller"] = next(iter(day_callers))
        else:
            rec["caller"] = UNATTRIBUTED

    calls_to = defaultdict(list)            # number -> [(when, caller)] outbound, for phone statuses
    calls_to_owner = defaultdict(list)      # owner -> outbound calls, for --detail
    for rec in calls:
        dt, day = rec["at"], rec["at"].date().isoformat()
        if rec["inbound"]:
            picked_up = rec["answered"] and not rec["noanswer"]
            bump(rec["caller"] or UNATTRIBUTED, day, "inbound_answered" if picked_up else "inbound_missed")
            continue
        email = rec["caller"]
        calls_to[rec["number"]].append((dt, email))
        if email.lower() not in excluded:
            calls_to_owner[rec["owner"]].append(rec)
        bump(email, day, "dials")
        if email.lower() not in excluded:
            for scope in (acct, per[email]):
                scope["records"].add(rec["owner"])
                scope["days"].add(day)
                if scope["first_call"] is None or dt < scope["first_call"]:
                    scope["first_call"] = dt
                if scope["last_call"] is None or dt > scope["last_call"]:
                    scope["last_call"] = dt
        if rec["answered"]:
            bump(email, day, "answered")
            dur = rec["duration"]
            bump(email, day, "talk_seconds", dur)
            if dur >= mean_s:
                bump(email, day, "meaningful_conversations")
                bump(email, day, "conversations")
            elif dur >= conv_s:
                bump(email, day, "conversations")
            elif dur >= vm_s:
                bump(email, day, "band_brief")
            else:
                bump(email, day, "band_vm")
        elif rec["noanswer"]:
            bump(email, day, "noanswer")

    def caller_before(number: str, dt) -> str:
        """The caller of the latest outbound call to this number at or before dt, the same day."""
        best = None
        for when, email in calls_to.get(number, ()):
            if when <= dt and when.date() == dt.date() and (best is None or when > best[0]):
                best = (when, email)
        return best[1] if best else UNATTRIBUTED

    # 2. Texts, phone statuses, record statuses and completed tasks.
    phone_events = [e for _, e in events if e.get("event_type") == PHONE_STATUS]
    prop_events = [e for _, e in events if e.get("event_type") == PROPERTY_STATUS]
    p_idx = detect_new_index(phone_events, "phone")
    s_idx = detect_new_index(prop_events, "property")
    phone_final, prop_moves = {}, defaultdict(list)
    for dt, ev in events:
        et, day = ev.get("event_type"), dt.date().isoformat()
        payload = ev.get("payload") or {}
        if et == SMS_SENT:
            who = person_of((payload.get("sms") or {}).get("external_user"))
            who = who if who[0] else author_of(ev)
            if who[0]:
                names.setdefault(who[0], who[1])
            bump(who[0] or UNATTRIBUTED, day, "sms_sent")
        elif et == SMS_RECEIVED:
            if UNATTRIBUTED not in excluded:
                acct["sms_received"] += 1
        elif et == PHONE_STATUS:
            number = digits((payload.get("owner") or {}).get("phone"))
            _, ns = status_pair(ev, "phone", p_idx)
            if number and ns:
                who = author_of(ev)
                email = who[0] or caller_before(number, dt)
                if who[0]:
                    names.setdefault(who[0], who[1])
                prev = phone_final.get(number)
                if prev is None or dt >= prev[0]:
                    phone_final[number] = (dt, ns, email)
        elif et == PROPERTY_STATUS:
            old, ns = status_pair(ev, "property", s_idx)
            if ns:
                who = author_of(ev)
                if who[0]:
                    names.setdefault(who[0], who[1])
                prop_moves[ev.get("resource") or ""].append((dt, old, ns, who[0] or UNATTRIBUTED))
        elif et == TASK_DONE:
            title = ((payload.get("task") or {}).get("title") or "").lower()
            if any(k in title for k in ("appoint", "appt", "meeting", "consult")):
                who = author_of(ev)
                if who[0]:
                    names.setdefault(who[0], who[1])
                bump(who[0] or UNATTRIBUTED, day, "appointments")

    for number, (dt, ns, email) in phone_final.items():
        day = dt.date().isoformat()
        if ns in CORRECT_STATES:
            bump(email, day, "correct_numbers")
        elif ns in WRONG_STATES:
            bump(email, day, "wrong_numbers")
        elif ns == "DEAD":
            bump(email, day, "dead_numbers")
        elif ns == "DNC":
            bump(email, day, "dnc_numbers")

    # A lead is a record ENTERING a lead status from outside one, counted once a record. A move
    # between two lead statuses (New Lead to Hot Lead) is the same lead, never a second one.
    prop_final = {}
    for uuid, moves in prop_moves.items():
        moves.sort(key=lambda m: m[0])
        entered = next((m for m in moves if str(m[2]).lower() in lead_set
                        and str(m[1] or "").lower() not in lead_set), None)
        if entered:
            bump(entered[3], entered[0].date().isoformat(), "leads")
        dt, old, ns, email = moves[-1]
        prop_final[uuid] = (dt, ns, email, entered is not None)
        if ns == "not_interested":
            bump(email, dt.date().isoformat(), "not_interested")
        elif ns == "follow_up":
            bump(email, dt.date().isoformat(), "follow_ups")

    for email in list(per):
        if email.lower() in excluded:
            per.pop(email)
    return {"from": day_from, "to": day_to, "account_totals": acct, "callers": dict(per),
            "daily": dict(daily), "names": names, "calls_to_owner": dict(calls_to_owner),
            "prop_final": prop_final}


def address_text(a) -> str:
    if isinstance(a, dict):
        tail = " ".join(x for x in (a.get("state"), a.get("postal_code") or a.get("zip5")) if x)
        return ", ".join(x for x in (a.get("street"), a.get("city"), tail) if x)
    return str(a or "")


def build_detail(res: dict, rows_by_owner: dict, rows_by_uuid: dict) -> list[dict]:
    """One row per record worked: the owner's calls (a call is to the owner, so an owner with three
    houses shows the same calls on each) and the record's own status change in the period."""
    names = res["names"]
    details, done = [], set()

    def row_for(r: dict, owner_calls: list) -> dict:
        d = {"address": address_text(r.get("address")), "status": r.get("status") or "",
             "dials": 0, "answered": 0, "talk_seconds": 0, "callers": set(),
             "status_set_to": "", "status_set_by": "", "is_lead": False, "last_activity": ""}
        last = None
        for rec in owner_calls:
            d["dials"] += 1
            d["answered"] += 1 if rec["answered"] else 0
            d["talk_seconds"] += rec["duration"] if rec["answered"] else 0
            d["callers"].add(names.get(rec["caller"], rec["caller"]))
            last = rec["at"] if last is None or rec["at"] > last else last
        pf = res["prop_final"].get(r.get("uuid"))
        if pf:
            dt, ns, email, entered = pf
            d["status_set_to"], d["status_set_by"], d["is_lead"] = ns, names.get(email, email), entered
            last = dt if last is None or dt > last else last
        d["last_activity"] = last.strftime("%Y-%m-%d %H:%M") if last else ""
        return d

    for owner, owner_calls in res["calls_to_owner"].items():
        for r in rows_by_owner.get(owner, []):
            if r.get("uuid") in done:
                continue
            done.add(r.get("uuid"))
            details.append(row_for(r, owner_calls))
    for uuid in res["prop_final"]:
        if uuid in done or uuid not in rows_by_uuid:
            continue
        done.add(uuid)
        details.append(row_for(rows_by_uuid[uuid], []))
    details.sort(key=lambda d: (-d["is_lead"], -d["dials"]))
    return details


def pull(key: str, day_from: str, day_to: str, tz, bench: dict, detail: bool = False) -> dict:
    start = datetime.datetime.combine(datetime.date.fromisoformat(day_from), datetime.time(0), tz)
    end = datetime.datetime.combine(datetime.date.fromisoformat(day_to) + datetime.timedelta(days=1),
                                    datetime.time(0), tz)
    log(f"reading the account activity log, {day_from} to {day_to} ({tz.key})...")
    raw = read_log(key, start, end)
    log(f"{len(raw)} events read")
    res = count(raw, day_from, day_to, tz, bench)
    res["records_detail"] = []
    if detail:
        owners = [o for o in res["calls_to_owner"] if o]
        log(f"reading the records of {len(owners)} owners worked, for --detail...")
        rows_by_owner = defaultdict(list)
        for r in records_by(key, "owners", owners):
            rows_by_owner[((r.get("owner") or {}).get("uuid") or "")].append(r)
        have = {r.get("uuid") for rows in rows_by_owner.values() for r in rows}
        rest = [u for u in res["prop_final"] if u and u not in have]
        rows_by_uuid = {r.get("uuid"): r for rows in rows_by_owner.values() for r in rows}
        for r in records_by(key, "properties", rest):
            rows_by_uuid[r.get("uuid")] = r
        res["records_detail"] = build_detail(res, rows_by_owner, rows_by_uuid)
    return res


# ---- rendering ----
def fmt_hms(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "0.0%"


def caller_order(res):
    return sorted(res["callers"].items(), key=lambda kv: (kv[0] == UNATTRIBUTED, -kv[1]["dials"]))


def render_md(res: dict, bench: dict) -> str:
    a = res["account_totals"]
    lead_lo, lead_hi = bench["leads_per_caller_day"]
    c_lo, c_hi = bench["leads_per_contract"]
    take = bench["appointment_take_rate"]
    dials = a["dials"]
    out = [f"# KPI Report, {res['from']} to {res['to']}", "",
           f"- Dials: {dials}  |  Answered: {a['answered']} ({pct(a['answered'], dials)})",
           f"- Conversations 60s+: {a['conversations']} ({pct(a['conversations'], dials)})  |  "
           f"Meaningful 120s+: {a['meaningful_conversations']}",
           f"- Correct numbers: {a['correct_numbers']} ({pct(a['correct_numbers'], dials)} right-party)"
           f"  |  Wrong {a['wrong_numbers']}  Dead {a['dead_numbers']}  DNC {a['dnc_numbers']}",
           f"- Leads: {a['leads']}  |  Not interested: {a['not_interested']}  |  "
           f"Follow-ups: {a['follow_ups']}  |  Appointments logged: {a['appointments']}",
           f"- Talk time: {fmt_hms(a['talk_seconds'])}  |  Texts: {a['sms_sent']} out / {a['sms_received']} in",
           f"- Inbound calls: {a['inbound_answered']} answered, {a['inbound_missed']} missed",
           "", "## By caller", "",
           "| Caller | Days | Dials | Dials/day | Ans% | Convos | Correct | NI | Leads | Floor |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for email, c in caller_order(res):
        if not any(c[k] for k in ("dials", "correct_numbers", "leads", "not_interested")):
            continue
        nd = len(c["days"]) or 1
        floor = bench["dials_floor_per_caller"] * len(c["days"])
        mark = "" if email == UNATTRIBUTED else ("MET" if c["dials"] >= floor and floor else "BELOW")
        out.append(f"| {res['names'].get(email, email)} | {len(c['days'])} | {c['dials']} | "
                   f"{c['dials'] / nd:.0f} | {pct(c['answered'], c['dials'])} | {c['conversations']} | "
                   f"{c['correct_numbers']} | {c['not_interested']} | {c['leads']} | {mark} |")
    if UNATTRIBUTED in res["callers"]:
        out += ["", "Unattributed: work no event names a caller for and no rule could place (more than "
                    "one caller on the same line and day, or a status set by a sequence). It still counts "
                    "in the account totals."]
    out += ["", "## Daily trend", "", "| Day | Dials | Answered | Convos | Correct | NI | Leads |",
            "|---|---|---|---|---|---|---|"]
    for day in sorted(res["daily"]):
        d = res["daily"][day]
        out.append(f"| {day} | {d['dials']} | {d['answered']} | {d['conversations']} | "
                   f"{d['correct_numbers']} | {d['not_interested']} | {d['leads']} |")
    dpc = f"{dials / a['correct_numbers']:.1f}" if a["correct_numbers"] else "n/a"
    out += ["", "## Funnel and pacing", "",
            f"- Dials per correct number: {dpc} (scored target ~{bench['dials_per_correct_scored']}, "
            f"blind ~{bench['dials_per_correct_blind']})",
            f"- Correct toward next deal: {a['correct_numbers']} / {bench['correct_numbers_per_deal']}",
            f"- Leads: {a['leads']} (baseline {lead_lo}-{lead_hi}/caller/day)  ->  "
            f"~{round(a['leads'] * take)} appointments at {take * 100:.0f}% take  ->  "
            f"{a['leads'] / c_hi:.1f}-{a['leads'] / c_lo:.1f} contracts at 1 per {c_lo}-{c_hi} leads"]
    return "\n".join(out)


def write_csv(res: dict, path: Path) -> None:
    fields = ["caller", "days", "dials", "answered", "conversations",
              "meaningful_conversations", "correct_numbers", "wrong_numbers",
              "not_interested", "leads", "appointments", "talk_seconds", "sms_sent"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for email, c in caller_order(res):
            w.writerow([res["names"].get(email, email), len(c["days"])] +
                       [c[f] for f in fields[2:]])


def write_xlsx(res: dict, bench: dict, path: Path) -> bool:
    try:
        from openpyxl import Workbook
    except ImportError:
        log("openpyxl not installed; skipping Excel (pip install openpyxl)")
        return False
    wb = Workbook()
    ws = wb.active
    ws.title = "By Caller"
    ws.append(["Caller", "Days", "Dials", "Answered", "Convos", "Correct", "NI", "Leads", "Talk"])
    for email, c in caller_order(res):
        ws.append([res["names"].get(email, email), len(c["days"]), c["dials"], c["answered"],
                   c["conversations"], c["correct_numbers"], c["not_interested"], c["leads"],
                   fmt_hms(c["talk_seconds"])])
    ws2 = wb.create_sheet("Daily")
    ws2.append(["Day", "Dials", "Answered", "Convos", "Correct", "NI", "Leads"])
    for day in sorted(res["daily"]):
        d = res["daily"][day]
        ws2.append([day, d["dials"], d["answered"], d["conversations"],
                    d["correct_numbers"], d["not_interested"], d["leads"]])
    if res.get("records_detail"):
        wsr = wb.create_sheet("Records Detail")
        wsr.append(["Address", "Current status", "Dials", "Answered", "Talk", "Callers",
                    "Status set to", "Set by", "Lead", "Last activity"])
        for d in res["records_detail"]:
            wsr.append([d["address"], d["status"], d["dials"], d["answered"],
                        fmt_hms(d["talk_seconds"]), ", ".join(sorted(d["callers"])),
                        d["status_set_to"], d["status_set_by"],
                        "YES" if d["is_lead"] else "", d["last_activity"]])
    ws3 = wb.create_sheet("Benchmarks")
    for k, v in bench.items():
        ws3.append([k, json.dumps(v) if isinstance(v, (list, dict)) else v])
    wb.save(path)
    return True


def post_slack(res: dict, webhook: str) -> None:
    a = res["account_totals"]
    text = (f"KPI Report {res['from']} to {res['to']}: {a['dials']} dials, "
            f"{a['conversations']} conversations, {a['correct_numbers']} correct numbers, "
            f"{a['not_interested']} not interested, {a['leads']} leads, "
            f"talk {fmt_hms(a['talk_seconds'])}")
    r = urllib.request.Request(webhook, data=json.dumps({"text": text}).encode(),
                               headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(r, timeout=15):
        pass
    log("Slack digest posted")


# ---- selftest: the counting rules on invented events, no network ----
def selftest() -> int:
    tz = ZoneInfo("America/New_York")
    bench = dict(DEFAULT_BENCHMARKS)
    bench["excluded_callers"] = ["admin@example.com"]
    n = [0]

    def ev(et, ts, resource="", **payload):
        n[0] += 1
        return {"id": f"e{n[0]}", "event_type": et, "timestamp": ts, "resource": resource, "payload": payload}

    def call(uuid, line, number, direction="outbound", duration=None, user=None):
        return {"uuid": uuid, "origin_number": line if direction == "outbound" else number,
                "destination_number": number if direction == "outbound" else line,
                "direction": direction, "duration": duration, "external_user": user or {}}

    amy = {"email": "amy@example.com", "name": "Amy"}
    bo = {"email": "bo@example.com", "name": "Bo"}
    adm = {"email": "admin@example.com", "name": "Admin"}
    L1, L2, L3, L4 = "8650000001", "8650000002", "8650000003", "8650000004"
    events = [
        # Amy's dial names nobody; its answer names her (Kixie shape). One call, not two.
        ev(MADE, "2026-07-06 14:00:00", "o1", call=call("c1", L1, "5550000101")),
        ev(ANSWERED, "2026-07-06 14:00:40", "o1", call=call("c1", L1, "5550000101", duration=130, user=amy)),
        # A second dial on Amy's line names nobody and has no answer: the line places it with Amy.
        ev(MADE, "2026-07-06 14:05:00", "o2", call=call("c2", L1, "5550000102")),
        ev(NOANSWER, "2026-07-06 14:05:30", "o2", call=call("c2", L1, "5550000102")),
        # Bo on his own line, answered under a minute: answered but not a conversation.
        ev(MADE, "2026-07-06 15:00:00", "o3", call=call("c3", L2, "5550000103", user=bo)),
        ev(ANSWERED, "2026-07-06 15:00:20", "o3", call=call("c3", L2, "5550000103", duration=20, user=bo)),
        # An answer whose id matches no dial joins the open dial to the same owner and number.
        ev(MADE, "2026-07-06 15:10:00", "o4", call=call("c4", L2, "5550000104", user=bo)),
        ev(ANSWERED, "2026-07-06 15:10:30", "o4", call=call("c4-other", L2, "5550000104", duration=70, user=bo)),
        # Two callers worked the day and this line is new: nobody can be placed.
        ev(MADE, "2026-07-06 16:00:00", "o5", call=call("c5", L3, "5550000105")),
        # The admin's own test dial stays out of every table.
        ev(MADE, "2026-07-06 16:30:00", "o6", call=call("c6", L4, "5550000106", user=adm)),
        # An inbound call is not a dial.
        ev(RECEIVED, "2026-07-06 17:00:00", "o1", call=call("c7", L1, "5550000101", "inbound", 45)),
        # Kixie: a ring with no duration, then "missed" on the same id, is a missed inbound call.
        ev(RECEIVED, "2026-07-06 17:20:00", "o7", call=call("c9", L1, "5550000107", "inbound")),
        ev(MISSED, "2026-07-06 17:20:25", "o7", call=call("c9", L1, "5550000107", "inbound", 1)),
        # Kixie: an outbound call nobody answered arrives as "missed" on the dial's own id.
        ev(MADE, "2026-07-06 14:30:00", "o8", call=call("c10", L1, "5550000108")),
        ev(MISSED, "2026-07-06 14:30:21", "o8", call=call("c10", L1, "5550000108", duration=21)),
        # ...and when that miss carries another id, it joins the open dial, never a second call.
        ev(MADE, "2026-07-06 15:20:00", "o9", call=call("c11", L2, "5550000109", user=bo)),
        ev(MISSED, "2026-07-06 15:20:30", "o9", call=call("c11-other", L2, "5550000109", duration=3)),
        # A dialer marks the number Amy reached CORRECT: the correct number is Amy's.
        ev(PHONE_STATUS, "2026-07-06 14:01:00", "o1", owner={"phone": "+1 (555) 000-0101",
                                                              "status": ["UNKNOWN", "CORRECT"]}),
        # A person moves a record New Lead, then Hot Lead: one lead, not two.
        dict(ev(PROPERTY_STATUS, "2026-07-06 14:02:00", "p1", property={"status": ["prospecting", "new_lead"]}),
             author_extra_info={"email": "amy@example.com", "first_name": "Amy"}),
        dict(ev(PROPERTY_STATUS, "2026-07-06 18:00:00", "p1", property={"status": ["new_lead", "Hot Lead"]}),
             author_extra_info={"email": "amy@example.com", "first_name": "Amy"}),
        # A sequence sets Not Interested: unattributed, still in the account totals.
        ev(PROPERTY_STATUS, "2026-07-06 15:01:00", "p3", property={"status": ["prospecting", "not_interested"]}),
        # Outside the window (next day, Eastern): ignored.
        ev(MADE, "2026-07-07 14:00:00", "o1", call=call("c8", L1, "5550000101", user=amy)),
    ]
    events.append(dict(events[0]))          # the same row read twice is counted once
    res = count(events, "2026-07-06", "2026-07-06", tz, bench)
    a, per = res["account_totals"], res["callers"]
    checks = [
        ("account dials (admin out, duplicate row once, next day out)", a["dials"], 7),
        ("account answered", a["answered"], 3),
        ("account no answer (outbound misses included)", a["noanswer"], 3),
        ("account conversations 60s+", a["conversations"], 2),
        ("Amy dials (her line places the unnamed dials)", per["amy@example.com"]["dials"], 3),
        ("Bo dials (answers and misses joined to his dials)", per["bo@example.com"]["dials"], 3),
        ("Bo answered", per["bo@example.com"]["answered"], 2),
        ("unattributed dials", per[UNATTRIBUTED]["dials"], 1),
        ("admin left out of the caller table", "admin@example.com" in per, False),
        ("correct number credited to the caller who reached it", per["amy@example.com"]["correct_numbers"], 1),
        ("one lead for New Lead then Hot Lead", a["leads"], 1),
        ("lead credited to the person who set it", per["amy@example.com"]["leads"], 1),
        ("not interested counted in the account", a["not_interested"], 1),
        ("inbound answered", a["inbound_answered"], 1),
        ("inbound missed (a ring, then missed)", a["inbound_missed"], 1),
        ("talk seconds", a["talk_seconds"], 130 + 20 + 70),
    ]
    rows_by_owner = {"o1": [{"uuid": "p1", "owner": {"uuid": "o1"}, "status": "Hot Lead",
                             "address": {"street": "100 Example St", "city": "Testville",
                                         "state": "TN", "postal_code": "00000"}}]}
    det = build_detail(res, rows_by_owner, {})
    checks += [("detail row for the worked record", len(det), 1),
               ("detail address", det[0]["address"] if det else "", "100 Example St, Testville, TN 00000"),
               ("detail marks the lead", det[0]["is_lead"] if det else None, True)]
    bad = 0
    for label, got, want in checks:
        ok = got == want
        bad += 0 if ok else 1
        print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    print(f"{len(checks) - bad} of {len(checks)} passed")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DataSift KPI puller (kpi-engine skill), on your Open API key")
    ap.add_argument("--from", dest="day_from")
    ap.add_argument("--to", dest="day_to")
    ap.add_argument("--days", type=int, help="trailing N days ending today")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--xlsx", action="store_true")
    ap.add_argument("--detail", action="store_true",
                    help="also write a record-level detail CSV (one row per worked record)")
    ap.add_argument("--slack", metavar="WEBHOOK_URL")
    ap.add_argument("--show-benchmarks", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="check the counting rules on invented events")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    bench = load_benchmarks()
    if args.show_benchmarks:
        print(json.dumps(bench, indent=2))
        return 0
    tz = ZoneInfo(args.tz)
    today = datetime.datetime.now(tz).date()
    if args.days:
        day_from = (today - datetime.timedelta(days=args.days - 1)).isoformat()
        day_to = today.isoformat()
    elif args.day_from:
        day_from, day_to = args.day_from, args.day_to or today.isoformat()
    else:
        day_from = day_to = today.isoformat()

    key = api_key()
    try:
        res = pull(key, day_from, day_to, tz, bench, detail=args.detail)
    except RuntimeError as exc:
        # A pull that could not read the log is a failure, never a period of zeros.
        sys.exit(f"Could not read the activity log, so the period cannot be counted: {exc}")
    md = render_md(res, bench)
    print("\n" + md)
    stem = f"kpi_{day_from}_{day_to}"
    (HERE / f"{stem}.md").write_text(md, encoding="utf-8")
    write_csv(res, HERE / f"{stem}.csv")
    log(f"wrote {stem}.md and {stem}.csv")
    if args.detail:
        with open(HERE / f"{stem}_records.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["address", "current_status", "dials", "answered", "talk",
                        "callers", "status_set_to", "status_set_by", "is_lead", "last_activity"])
            for d in res["records_detail"]:
                w.writerow([d["address"], d["status"], d["dials"], d["answered"],
                            fmt_hms(d["talk_seconds"]), ", ".join(sorted(d["callers"])),
                            d["status_set_to"], d["status_set_by"],
                            "YES" if d["is_lead"] else "", d["last_activity"]])
        log(f"wrote {stem}_records.csv ({len(res['records_detail'])} records)")
    if args.xlsx and write_xlsx(res, bench, HERE / f"{stem}.xlsx"):
        log(f"wrote {stem}.xlsx")
    if args.slack:
        post_slack(res, args.slack)
    return 0


if __name__ == "__main__":
    sys.exit(main())
