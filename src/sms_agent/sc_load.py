"""Load a released text batch from the brain into Smarter Contact (Ty, 2026-11-18: "a fully
autonomous Smarter Contact campaign for all the new records that come in every day").

The brain decides who is texted: the gate, the registry rule, the limits, the lane timeline and
the series all run there, twice (when the batch is staged and again when it is released). This
module only carries the released load out, in the account the texting app already signs in to
(sc_client), so the Smarter Contact login never has to leave this box:

  1. every number becomes a contact: found by phone when Smarter Contact has it, created when it
     does not, with the record's id in the Sift Record field so a reply finds its record;
  2. each group in the load is created, filled and read back;
  3. each group gets ONE standard campaign on the templates the load names, starting at the
     group's send time, inside the 9am (noon for the other time zones) to 6pm Eastern weekday
     window, at the account's approved six texts a minute, and the campaign is started.

Nothing here types a message: Smarter Contact sends when the campaign starts. Replies come back
through the existing webhook (sc_inbound), exactly as they do for the Priority 1 campaigns.

REFUSED BEFORE ANYTHING IS WRITTEN, each with its reason: no approved sending number, a template the
load names that the account does not hold, or a load with no groups. (Smarter Contact's `isBlocked`
flag is not a reason: see _usable.) A group that already has a campaign is a load
sent twice and is skipped, never sent again.

    run(load, commit=False)   plan: what would be created, and why anything is refused
    run(load, commit=True)    create and start

The same route also takes the brain's do-not-contact jobs ("action": "revoke", see revoke_run): the
revocation stage's Smarter Contact half, which needs this box's login just as a load does.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, sc_client

log = logging.getLogger(__name__)

TZ_WINDOWS = {"east": 9, "other": 12}     # hour the campaign window opens, Eastern (the account clock)
WINDOW_END = 18
WEEKDAYS = [1, 2, 3, 4, 5]
PER_MIN = 6                               # the account's A2P approval reads maxTpm 6, per campaign
PER_TEMPLATE = 50                         # Priority 1 rule: one template for every 50 contacts, at most 8
MAX_TEMPLATES = 8
SIFT_KEY = "SIFT_RECORD"
STATE_DIR = Path(config.DATA_DIR) / "sc_loads"

Q_FIELDS = "query { findFields { id key name } }"
Q_PHONES = ("query P($f: OwnPhoneFilter, $p: Pagination) { findOwnPhones(filter: $f, pagination: $p) "
            "{ items { id status number isBlocked messagingEnabled } } }")
Q_TEMPLATES = ("query T($p: Pagination) { findMessageTemplates(pagination: $p) { hasNext nextId nextCreatedAt "
               "items { id name } } }")
Q_GROUP_BY_NAME = ("query G($filter: GroupsFilter, $pagination: Pagination) { findGroups(filter: $filter, "
                   "pagination: $pagination) { items { id name contacts campaigns { name } } } }")
Q_SUPP = ("query S($p: Pagination) { findSuppressions(pagination: $p) { hasNext nextId nextCreatedAt "
          "items { number } } }")
Q_CAMPAIGN = "query C($id: String!) { getCampaign(id: $id) { id name contacts state { status } } }"
M_CONTACT = "mutation C($input: CreateContactInput!) { createContact(input: $input) { id phone } }"
M_GROUP = "mutation CreateGroup($input: CreateGroupInput!) { createGroup(input: $input) { id name } }"
M_ADD = "mutation AddGroupToContacts($input: AddGroupToContactsInput!) { addGroupToContacts(input: $input) { success } }"
M_CAMPAIGN = "mutation CreateCampaign($input: CreateCampaignInput!) { createCampaign(input: $input) { id name } }"
M_START = "mutation StartCampaign($id: String!) { startCampaign(id: $id) { success } }"

_running: set[int] = set()
_lock = threading.Lock()


class Refused(RuntimeError):
    """The load cannot go out as it is; nothing was written."""


def digits10(n) -> str:
    d = "".join(ch for ch in str(n or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else ""


MAX_PAGES = 100


def _pages(query: str, key: str, variables: dict | None = None, limit: int = 100, sc=None) -> list:
    """Every row of a list. Smarter Contact's cursor cannot be trusted to end (measured 2026-11-18:
    the group list returned the same 25 rows with the same next id forever, and the suppression list
    kept saying there was more after an empty page), so paging stops at an empty page or a page with
    nothing new, and at MAX_PAGES whatever the server says."""
    sc = sc or sc_client
    rows, page, seen = [], {"limit": limit}, set()
    for _ in range(MAX_PAGES):
        d = (sc.gql(query, dict(variables or {}, p=page)) if "$p" in query
             else sc.gql(query, dict(variables or {}, pagination=page))).get(key) or {}
        items = d.get("items") or []
        fresh = [x for x in items if json.dumps(x, sort_keys=True, default=str) not in seen]
        seen.update(json.dumps(x, sort_keys=True, default=str) for x in fresh)
        rows += fresh
        if not fresh or not d.get("hasNext") or not d.get("nextId"):
            break
        page = {"limit": limit, "nextId": d["nextId"], "nextCreatedAt": d.get("nextCreatedAt")}
    return rows


def find_group(name: str, sc=None) -> dict:
    """The group with exactly this name ({} when none). The `name` filter is a contains-search, so
    the exact name is picked from what it returns; the group list itself is never paged."""
    sc = sc or sc_client
    d = sc.gql(Q_GROUP_BY_NAME, {"filter": {"name": name}, "pagination": {"limit": 25}}).get("findGroups") or {}
    return next((g for g in d.get("items") or [] if g.get("name") == name), {})


def _usable(p: dict) -> bool:
    """A number a campaign may send from: the status the app's Phone Numbers page shows (Approved)
    and messaging on. NOT `isBlocked`: on 2026-11-18 all ten numbers read isBlocked true while the
    page showed every one Approved and the auto replies had sent 63 texts from them since 11-11, so
    that flag does not mean the number cannot send (the Priority 1 launcher's filter assumed it did)."""
    return p.get("status") == "APPROVED" and p.get("messagingEnabled", True) is not False


def sending_phones(sc=None) -> list[str]:
    """The numbers a campaign may send from (see _usable)."""
    sc = sc or sc_client
    items = (sc.gql(Q_PHONES, {"f": {"scope": "MESSAGE"}, "p": {"limit": 50}}).get("findOwnPhones") or {}).get("items") or []
    return [p["id"] for p in items if _usable(p)]


def phone_report(sc=None) -> dict:
    sc = sc or sc_client
    items = (sc.gql(Q_PHONES, {"f": {"scope": "MESSAGE"}, "p": {"limit": 50}}).get("findOwnPhones") or {}).get("items") or []
    return {"numbers": len(items), "usable": sum(1 for p in items if _usable(p)),
            "not approved": sum(1 for p in items if p.get("status") != "APPROVED")}


def field_keys(sc=None) -> dict:
    """key -> field id, with the Sift Record custom field (which has no key) under SIFT_RECORD."""
    sc = sc or sc_client
    out = {}
    for f in sc.gql(Q_FIELDS).get("findFields") or []:
        if f.get("key"):
            out[f["key"]] = f["id"]
        elif (f.get("name") or "").strip().casefold() == sc_client.SIFT_RECORD_FIELD.casefold():
            out[SIFT_KEY] = f["id"]
    return out


def contact_input(c: dict, keys: dict) -> dict:
    return {"phone": "+1" + digits10(c["phone"]), "firstName": c.get("first_name") or "",
            "lastName": c.get("last_name") or "",
            "attrs": [{"fieldId": keys[k], "value": v} for k, v in (c.get("values") or {}).items() if v and k in keys]}


def templates_for(group: dict, have: dict) -> tuple[list[str], list[str]]:
    """(template ids, missing titles) for one group. Smarter Contact rotates a campaign's templates,
    and the Priority 1 rule is one template for every 50 contacts, so a group whose load names fewer
    than it needs uses every title it has and says so in the result."""
    titles = list(group.get("templates") or [])
    missing = [t for t in titles if t not in have]
    return [have[t] for t in titles if t in have], missing


def campaign_input(group: dict, gid: str, phones: list[str], template_ids: list[str], now: datetime) -> dict:
    start = datetime.fromisoformat(str(group.get("start") or now.isoformat()).replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    start = max(start, now)
    hour = TZ_WINDOWS.get(str(group.get("tz") or ""), 12)
    return {"input": {
        "name": group["name"], "type": "STANDARD", "groups": [gid], "ownPhones": phones,
        "sequences": [{"type": "SMS", "templates": template_ids, "hasCustomTimeRestrictions": True,
                       "timeRange": {"from": {"hour": hour, "minute": 0}, "to": {"hour": WINDOW_END, "minute": 0}},
                       "days": WEEKDAYS}],
        "scheduleOptions": {"startAt": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                            "hasCustomStartDate": True, "hasCustomBatchSize": True,
                            "perBatch": PER_MIN, "interval": 1, "frequency": "MINUTELY"}}}


def check(load: dict) -> list[str]:
    """What is wrong with a load's shape (empty means it may be read)."""
    out = []
    if not isinstance(load, dict):
        return ["the load is not an object"]
    if not isinstance(load.get("batch"), int):
        out.append("the load has no batch number")
    if not load.get("groups"):
        out.append("the load names no group")
    for c in load.get("contacts") or []:
        if not digits10(c.get("phone")):
            out.append("a contact has no ten-digit phone")
            break
    return out


def run(load: dict, commit: bool = False, *, sc=None, now: datetime | None = None, pause: float = 0.2) -> dict:
    """Carry one released load out. Dry unless commit. Returns {ok, groups: [...], ...}; a refusal
    returns ok False with `refused` and writes nothing."""
    sc = sc or sc_client
    now = now or datetime.now(timezone.utc)
    out = {"ok": False, "batch": load.get("batch") if isinstance(load, dict) else None, "dry": not commit,
           "groups": [], "contacts_new": 0, "contacts_found": 0, "skipped": {}, "problems": [],
           "loaded": [],         # the ten-digit numbers now in a started campaign: only these count as texted
           "texted_before": []}  # the ten-digit numbers left out of text 1 because their thread already holds a
    #                              text: the brain remembers each one and never stages text 1 to it again
    bad = check(load)
    if bad:
        out["refused"] = "; ".join(bad)
        return out
    skipped: dict[str, int] = {}

    def skip(why: str, n: int = 1) -> None:
        skipped[why] = skipped.get(why, 0) + n

    try:
        phones = sending_phones(sc)
        if not phones:
            rep = phone_report(sc)
            why = "no Smarter Contact number can send (%d numbers, %d not approved)" % (rep["numbers"], rep["not approved"])
            if commit:
                raise Refused(why + ": nothing was loaded")
            out["problems"].append(why + "; a real load would be refused here")   # a dry run keeps planning
        have = {t["name"]: t["id"] for t in _pages(Q_TEMPLATES, "findMessageTemplates", sc=sc)}
        missing = sorted({t for g in load["groups"] for t in templates_for(g, have)[1]})
        if missing:
            raise Refused("the account has no template named %s: nothing was loaded" % ", ".join(missing[:4]))
        if any(not g.get("templates") for g in load["groups"]):
            raise Refused("a group in the load names no template: nothing was loaded")
        keys = field_keys(sc)
        if SIFT_KEY not in keys:
            raise Refused("the Sift Record field is missing in Smarter Contact, so a reply could not find its record")
        groups_now = {}
        for g in load["groups"]:
            hit = find_group(g["name"], sc=sc)
            if hit:
                groups_now[g["name"]] = hit
        suppressed = {digits10(s.get("number")) for s in _pages(Q_SUPP, "findSuppressions", sc=sc)}
    except Refused as exc:
        out["refused"] = str(exc)
        return out
    except sc_client.SCError as exc:
        out["refused"] = "Smarter Contact did not answer (%s): nothing was loaded" % str(exc)[:160]
        return out

    # One phone is one contact: find or create each once.
    by_group: dict[str, list[str]] = {}
    ids: dict[str, str] = {}
    gmeta = {g["name"]: g for g in load["groups"]}
    # "The Top 1,000 that we haven't texted" (Ty, 2026-11-18): the October Priority 1 campaigns texted
    # thousands of these owners from Smarter Contact itself, and DataSift's text counter does not know it.
    # So a text 1 never goes to a number whose Smarter Contact thread already holds an outgoing text;
    # texts 2 and 3 follow their own thread and are not checked.
    skip_texted = bool(load.get("skip_texted", True))
    for c in load.get("contacts") or []:
        n = digits10(c["phone"])
        g = c.get("group") or ""
        if not g:
            skip("no group (an open thread: typed, not loaded)")
            continue
        if n in suppressed:
            skip("on Smarter Contact's own do-not-text list")
            continue
        if g in groups_now and (groups_now[g].get("campaigns") or []):
            skip("the group already has a campaign (a load sent twice)")
            continue
        if n not in ids:
            try:
                cid = sc.contact_id_for(n)
            except sc_client.SCError as exc:
                out["problems"].append("phone lookup failed for a number ending %s (%s)" % (n[-4:], str(exc)[:80]))
                continue
            if cid:
                meta = gmeta.get(g) or {}
                if skip_texted and str(meta.get("series") or "opening") == "opening" and int(meta.get("step") or 1) == 1:
                    try:
                        texted = any(m.get("dir") == "out" for m in sc.thread(cid, limit=5))
                    except sc_client.SCError as exc:
                        out["problems"].append("thread read failed for a number ending %s (%s); left out" % (n[-4:], str(exc)[:80]))
                        continue
                    if texted:
                        skip("texted before in Smarter Contact (text 1 goes only to a number with no thread)")
                        out["texted_before"].append(n)
                        ids[n] = ""
                        continue
                out["contacts_found"] += 1
            elif commit:
                try:
                    cid = (sc.gql(M_CONTACT, {"input": contact_input(c, keys)}).get("createContact") or {}).get("id") or ""
                except sc_client.SCError as exc:
                    out["problems"].append("contact refused for a number ending %s (%s)" % (n[-4:], str(exc)[:100]))
                    continue
                out["contacts_new"] += 1
                time.sleep(pause)
            else:
                cid = "new:%s" % n[-4:]
                out["contacts_new"] += 1
            if not cid:
                out["problems"].append("no contact id came back for a number ending %s" % n[-4:])
                continue
            ids[n] = cid
        if not ids.get(n):              # a number left out above (texted before) stays out of every group
            continue
        by_group.setdefault(g, [])
        if ids[n] not in by_group[g]:
            by_group[g].append(ids[n])

    for g in sorted(load["groups"], key=lambda x: x["name"]):
        name = g["name"]
        members = by_group.get(name) or []
        row = {"group": name, "contacts": len(members), "start": g.get("start"), "tz": g.get("tz"),
               "templates": len(g.get("templates") or []), "campaign": "", "state": ""}
        if not members:
            row["state"] = "nothing to send"
            out["groups"].append(row)
            continue
        tids, _ = templates_for(g, have)
        need = min(math.ceil(len(members) / PER_TEMPLATE), MAX_TEMPLATES)
        if len(tids) < need:
            out["problems"].append("%s has %d contacts and %d templates (Smarter Contact likes %d)" % (name, len(members), len(tids), need))
        if not commit:
            row["state"] = "would create the group and a campaign starting %s" % (g.get("start") or "now")
            out["groups"].append(row)
            continue
        try:
            gid = (groups_now.get(name) or {}).get("id") or \
                (sc.gql(M_GROUP, {"input": {"name": name}}).get("createGroup") or {}).get("id")
            for k in range(0, len(members), 200):
                res = sc.gql(M_ADD, {"input": {"groupId": gid, "ids": members[k:k + 200]}})
                if not (res.get("addGroupToContacts") or {}).get("success"):
                    raise sc_client.SCError("adding contacts to %s was refused" % name)
            time.sleep(max(pause, 2.0) if pause else 0)
            back = find_group(name, sc=sc)
            if int(back.get("contacts") or 0) < len(members):
                raise sc_client.SCError("group %s read back %s of %d contacts" % (name, back.get("contacts"), len(members)))
            made = sc.gql(M_CAMPAIGN, campaign_input(g, gid, phones, tids, now)).get("createCampaign") or {}
            cid = made.get("id") or ""
            if not cid:
                raise sc_client.SCError("no campaign id came back for %s" % name)
            started = (sc.gql(M_START, {"id": cid}).get("startCampaign") or {}).get("success")
            state = ((sc.gql(Q_CAMPAIGN, {"id": cid}).get("getCampaign") or {}).get("state") or {}).get("status") or ""
            row.update(campaign=cid, state=state or ("started" if started else "created, not started"))
            if started:
                phone_of = {v: k for k, v in ids.items()}
                out.setdefault("loaded", []).extend(phone_of[m] for m in members if m in phone_of)
            else:
                out["problems"].append("campaign %s was created but did not start" % name)
        except sc_client.SCError as exc:
            row["state"] = "failed"
            out["problems"].append("%s: %s" % (name, str(exc)[:160]))
        out["groups"].append(row)
        time.sleep(pause)
    out["skipped"] = skipped
    out["ok"] = not any(r["state"] == "failed" for r in out["groups"])
    out["texts"] = sum(r["contacts"] for r in out["groups"])
    return out


# ---------------------------------------------------------------------- the route's side
def _path(batch: int) -> Path:
    return STATE_DIR / ("batch_%d.json" % int(batch))


def status(batch: int) -> dict:
    p = _path(batch)
    if not p.exists():
        return {"batch": int(batch), "status": "unknown"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"batch": int(batch), "status": "unknown"}


def _save(batch: int, doc: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(batch).with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(_path(batch))
            return
        except PermissionError:
            # Windows refuses to replace a file the status route has open at that moment (Linux never
            # does), and a result that is never saved reads "loading" forever. A short wait lets the
            # reader finish.
            if attempt == 19:
                raise
            time.sleep(0.05)


def accept(load: dict, dry: bool = False) -> dict:
    """Take a load from the brain and carry it out on a thread (the brain asks status() after). A
    batch already done or underway is never loaded twice: the brain may send it again after a
    timeout, and Smarter Contact must not get a second campaign for the same texts. A do-not-contact
    job from the revocation stage rides the same route and is told apart by its action."""
    if is_revoke(load):
        return accept_revoke(load, dry=dry)
    bad = check(load)
    if bad:
        return {"accepted": False, "why": "; ".join(bad)}
    batch = int(load["batch"])
    cur = status(batch)
    if cur.get("action") == REVOKE:
        return {"accepted": False, "why": "number %d holds a do-not-contact job, not a text load" % batch}
    if cur.get("status") in ("loading", "done") and not dry:
        return {"accepted": True, "already": cur.get("status"), "batch": batch}
    with _lock:
        if batch in _running:
            return {"accepted": True, "already": "loading", "batch": batch}
        _running.add(batch)
    doc = {"batch": batch, "status": "loading", "dry": dry, "received": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not dry:
        _save(batch, doc)

    def work() -> None:
        try:
            res = run(load, commit=not dry and not config.DRY_RUN)
            doc.update(status="done" if res.get("ok") else "failed", result=res,
                       finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001  (a crash is reported back, never a silent loss)
            log.exception("smarter contact load %s crashed", batch)
            doc.update(status="failed", result={"ok": False, "refused": "the loader crashed: %s" % str(exc)[:200]})
        finally:
            with _lock:
                _running.discard(batch)
        if dry:
            _DRY[batch] = doc
        else:
            _save(batch, doc)

    threading.Thread(target=work, daemon=True, name="sc-load-%d" % batch).start()
    return {"accepted": True, "batch": batch, "dry": dry}


_DRY: dict[int, dict] = {}


def dry_status(batch: int) -> dict:
    return _DRY.get(int(batch)) or {"batch": int(batch), "status": "loading" if int(batch) in _running else "unknown"}


# ---------------------------------------------------------------------- do-not-contact jobs (2026-11-19)
# The brain's revocation stage pulls owners who just became untouchable (opted out, sold, died, Do Not
# Market, a litigator) out of everything already in motion. Its Smarter Contact half needs this box's
# login, so the brain posts a job here with "action": "revoke", and this box does exactly what the stage
# does on a box that holds the login itself: each number is looked up by phone (contact_id_for) and,
# unless its owner just became a lead, the contact goes on Smarter Contact's own do-not-contact list
# (add_contact_dnc), so no campaign that is still sending texts it again. A new lead's numbers are only
# looked up: the lead manager still texts them one to one, and a person takes them out of a campaign.
#
# A job is never a load. It carries no groups and no contacts, so a copy of this module from before
# 2026-11-19 refuses it ("the load names no group") instead of building anything from it. Its number is
# at least REVOKE_MIN_ID (the brain uses milliseconds since 1970), far above any text batch number on
# the same status route, and its status document carries the job's own id, so a number used twice is
# refused rather than answered with another job's result.

REVOKE = "revoke"
REVOKE_MIN_ID = 1_000_000_000
REVOKE_MAX_FAILS = 3         # failures in a row before the rest of a job is left for the brain's next run
DRY_KEEP = 200               # dry answers kept in memory for the status route
# What each number's line says. The first five settle the number; would_mark is a dry run's answer;
# failed is tried again by the brain's next run.
OUTCOMES = ("marked", "already", "not_in_sc", "lead", "bad_number", "would_mark", "failed")


def is_revoke(payload) -> bool:
    return isinstance(payload, dict) and payload.get("action") == REVOKE


def check_revoke(job: dict) -> list[str]:
    """What is wrong with a do-not-contact job's shape (empty means it may be run)."""
    if not isinstance(job, dict):
        return ["the job is not an object"]
    out = []
    n = job.get("batch")
    if not isinstance(n, int) or isinstance(n, bool) or n < REVOKE_MIN_ID:
        out.append("the job's number must be a whole number of at least %d" % REVOKE_MIN_ID)
    if not str(job.get("job") or "").strip():
        out.append("the job has no id")
    if job.get("groups") or job.get("contacts"):
        out.append("a do-not-contact job carries no groups or contacts")
    owners = job.get("owners")
    if not isinstance(owners, list) or not owners:
        out.append("the job names no owner")
    elif any(not isinstance(o, dict) or not isinstance(o.get("numbers"), list) for o in owners):
        out.append("an owner in the job has no list of numbers")
    return out


def revoke_run(job: dict, commit: bool = False, *, sc=None, pause: float = 0.2) -> dict:
    """Carry one do-not-contact job out. Dry unless commit: a dry run looks every number up and marks
    nothing. Returns {ok, numbers: [{record, number, outcome, detail}], counts, problems}; a refusal
    returns ok False with `refused` and touches nothing."""
    sc = sc or sc_client
    out = {"ok": False, "action": REVOKE, "batch": job.get("batch") if isinstance(job, dict) else None,
           "job": job.get("job") if isinstance(job, dict) else None, "dry": not commit,
           "numbers": [], "counts": {}, "problems": []}
    bad = check_revoke(job)
    if bad:
        out["refused"] = "; ".join(bad)
        return out
    try:
        suppressed = {digits10(s.get("number")) for s in _pages(Q_SUPP, "findSuppressions", sc=sc)}
    except sc_client.SCError as exc:
        suppressed = set()
        out["problems"].append("could not read Smarter Contact's do-not-text list (%s); every number was looked up"
                               % str(exc)[:120])
    marked: set[str] = set()       # numbers this job already put on the list (one number on two records)
    contact: dict[str, str] = {}   # number -> contact id, looked up once per job
    fails, stopped = 0, ""
    for owner in job["owners"]:
        rec = str(owner.get("record") or "")
        mark = owner.get("dnc", True) is not False
        for raw in owner.get("numbers") or []:
            n = digits10(raw)
            row = {"record": rec, "number": n or str(raw)[:20], "outcome": "", "detail": ""}
            out["numbers"].append(row)
            if not n:
                row.update(outcome="bad_number", detail="not a ten-digit number")
                continue
            if n in suppressed or n in marked:
                row["outcome"] = "already"
                continue
            if stopped:
                row.update(outcome="failed", detail=stopped)
                continue
            try:
                if n not in contact:
                    contact[n] = sc.contact_id_for(n) or ""
                    if pause:
                        time.sleep(pause)
                cid = contact[n]
                if not cid:
                    row["outcome"] = "not_in_sc"
                elif not mark:
                    row["outcome"] = "lead"
                elif not commit:
                    row["outcome"] = "would_mark"
                else:
                    source = sc.add_contact_dnc(cid)
                    if pause:
                        time.sleep(pause)
                    marked.add(n)
                    row.update(outcome="already" if source == "already" else "marked", detail=str(source or ""))
                fails = 0
            except sc_client.SCError as exc:
                fails += 1
                row.update(outcome="failed", detail=str(exc)[:160])
                if fails >= REVOKE_MAX_FAILS:
                    stopped = "not tried: Smarter Contact failed %d times in a row" % fails
                    out["problems"].append("stopped after %d failures in a row (%s)" % (fails, str(exc)[:120]))
    counts: dict[str, int] = {}
    for row in out["numbers"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    out["counts"] = counts
    out["ok"] = not counts.get("failed")
    return out


def accept_revoke(job: dict, dry: bool = False) -> dict:
    """Take a do-not-contact job from the brain and carry it out on a thread, as accept() does a load.
    The same job sent twice (the brain asking again after a timeout) runs once; a number that already
    holds a text load or another job is refused, so one job can never be read as another's result."""
    bad = check_revoke(job)
    if bad:
        return {"accepted": False, "why": "; ".join(bad)}
    batch, jid = int(job["batch"]), str(job["job"])
    cur = (_DRY.get(batch) if dry else None) or status(batch)
    if cur.get("status") not in (None, "unknown"):
        if cur.get("action") != REVOKE:
            return {"accepted": False, "why": "number %d holds a text load, not a do-not-contact job" % batch}
        if str(cur.get("job") or "") != jid:
            return {"accepted": False, "why": "number %d already holds another do-not-contact job" % batch}
        if cur.get("status") in ("loading", "done"):
            return {"accepted": True, "already": cur["status"], "batch": batch, "action": REVOKE}
    with _lock:
        if batch in _running:
            return {"accepted": True, "already": "loading", "batch": batch, "action": REVOKE}
        _running.add(batch)
    doc = {"batch": batch, "action": REVOKE, "job": jid, "status": "loading", "dry": dry,
           "received": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if dry:
        # The brain's plan runs send a dry job every hour something changes, and dry answers live in
        # memory only, so the oldest are let go well before they could add up.
        while len(_DRY) >= DRY_KEEP:
            _DRY.pop(next(iter(_DRY)), None)
        _DRY[batch] = dict(doc)        # a copy: the route may read it while the thread fills the real one
    else:
        _save(batch, doc)

    def work() -> None:
        try:
            res = revoke_run(job, commit=not dry and not config.DRY_RUN)
            doc.update(status="done" if res.get("ok") else "failed", result=res,
                       finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        except Exception as exc:  # noqa: BLE001  (a crash is reported back, never a silent loss)
            log.exception("smarter contact do-not-contact job %s crashed", batch)
            doc.update(status="failed", result={"ok": False, "action": REVOKE, "job": jid,
                                                "refused": "the job crashed: %s" % str(exc)[:200]})
        finally:
            with _lock:
                _running.discard(batch)
        if dry:
            _DRY[batch] = doc
        else:
            _save(batch, doc)

    threading.Thread(target=work, daemon=True, name="sc-revoke-%d" % batch).start()
    return {"accepted": True, "batch": batch, "dry": dry, "action": REVOKE}
