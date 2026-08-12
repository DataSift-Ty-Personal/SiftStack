"""reisift / DataSift bridge.

Reuses the shared Deal Room `_api/clients` auth + CRM client rather than
re-implementing them, and degrades to a stated reason when that path is not
available (the receiver may run on a box that has no Deal Room checkout).

Every write here is gated on config.DRY_RUN so the whole agent can be run
end-to-end against production data without touching a record.
"""
from __future__ import annotations

import functools
import logging
import sys
from typing import Any, Optional

from . import config, store

log = logging.getLogger(__name__)

_UNAVAILABLE = ""


@functools.lru_cache(maxsize=1)
def _load():
    """Import the Deal Room client modules once. Returns (reisift_auth, CRMClient)."""
    global _UNAVAILABLE
    clients = config.DEALROOM_API / "clients"
    if not clients.exists():
        _UNAVAILABLE = f"Deal Room clients not found at {clients} (set DEALROOM_API_PATH)"
        return None, None
    if str(clients) not in sys.path:
        sys.path.insert(0, str(clients))
    try:
        import reisift_auth  # type: ignore
        from crm_api import CRMClient  # type: ignore
    except Exception as exc:  # noqa: BLE001 - any import failure degrades the same way
        _UNAVAILABLE = f"cannot import reisift clients: {exc}"
        return None, None
    return reisift_auth, CRMClient


@functools.lru_cache(maxsize=1)
def client():
    """A CRM client, or None.

    Prefers the shared Deal Room CRMClient (richer and better tested). Falls
    back to the self-contained `crm_standalone` client driven by REISIFT_API_KEY
    so a cloud box does not need a Deal Room checkout on disk.

    Pinning REISIFT_ACCOUNT matters on the shared path: the default
    `active_account` is the ~48h admin JWT, and when it expires every call
    throws while the run still exits zero. That is exactly the silent
    degradation the buyer sweep hit.
    """
    import os

    global _UNAVAILABLE
    auth, CRMClient = _load()
    if auth is not None:
        os.environ.setdefault("REISIFT_ACCOUNT", config.SIFT_ACCOUNT)
        try:
            if config.SIFT_IMPERSONATE:
                auth.start_impersonation(config.SIFT_IMPERSONATE)
            return CRMClient()
        except Exception as exc:  # noqa: BLE001
            _UNAVAILABLE = f"CRM auth failed: {exc}"

    from . import crm_standalone

    standalone = crm_standalone.from_env()
    if standalone is not None:
        log.info("using the standalone reisift client (REISIFT_API_KEY)")
        return standalone
    _UNAVAILABLE = (
        _UNAVAILABLE
        or "no Deal Room clients and no REISIFT_API_KEY; CRM writes are unavailable"
    )
    return None


def unavailable_reason() -> str:
    if client() is None:
        return _UNAVAILABLE or "CRM client unavailable"
    return ""


def _dry(action: str, **kw) -> dict:
    log.info("DRY_RUN crm.%s %s", action, kw)
    return {"_dry_run": True, "action": action, **kw}


# ------------------------------------------------------------------- reads

def get_record(record_uuid: str) -> Optional[dict]:
    c = client()
    if not c or not record_uuid:
        return None
    try:
        return c.get_record(record_uuid)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_record %s failed: %s", record_uuid, exc)
        return None


@functools.lru_cache(maxsize=1)
def _user_directory() -> dict:
    """uuid -> first name for the account's users.

    reisift publishes no documented user endpoint, so the config file is the
    source of truth and the API probe only fills gaps. A wrong name here signs
    a stranger's name to a real conversation, so an unresolved uuid must stay
    unresolved rather than be guessed.
    """
    directory = dict(config.senders())
    c = client()
    if not c:
        return directory
    for path in ("/api/internal/user/", "/api/internal/users/", "/api/internal/account/users/"):
        try:
            resp = c._request(path, params={"limit": 200})
        except Exception:  # noqa: BLE001 - probing undocumented routes
            continue
        rows = resp.get("results") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            uuid = row.get("uuid") or row.get("id")
            first = (row.get("first_name") or "").strip()
            if not first and row.get("name"):
                first = str(row["name"]).split()[0]
            if uuid and first and uuid not in directory:
                directory[uuid] = first
        if rows:
            break
    return directory


def sender_name_for(assigned_to: str) -> str:
    """The first name that signs this thread. Empty when we genuinely do not know."""
    if not assigned_to:
        return config.SENDER_NAME
    name = _user_directory().get(assigned_to, "")
    if name:
        return str(name).strip().split()[0]
    log.warning(
        "assigned_to %s has no name mapping; add it to %s", assigned_to, config.SENDERS_FILE
    )
    return config.SENDER_NAME


def deal_context(record_uuid: str) -> dict:
    """The facts the responder is allowed to use. Nothing derived, nothing guessed.

    Field names are the live record names, which are NOT the CSV upload names:
    estimate_value, equity_percent, last_sold, rental_value, year, etc.
    """
    rec = get_record(record_uuid)
    if not rec:
        return {}
    addr = rec.get("address") or {}
    owner = rec.get("owner") or {}
    # `assigned_to` is a bare uuid on the record, not a dict.
    assigned = rec.get("assigned_to")
    if isinstance(assigned, dict):
        assigned = assigned.get("uuid") or assigned.get("id") or ""
    ctx = {
        "record_uuid": record_uuid,
        "assigned_to": assigned or "",
        "assigned_name": sender_name_for(assigned or ""),
        "owner_first": owner.get("first_name") or "",
        "owner_last": owner.get("last_name") or "",
        "owner_company": owner.get("company") or "",
        "street": addr.get("street") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "zip": addr.get("postal_code") or "",
        "county": addr.get("county") or "",
        "vacant": addr.get("vacant"),
        "status": rec.get("status") or "",
        "beds": rec.get("bedrooms"),
        "baths": rec.get("bathrooms"),
        "sqft": rec.get("sqft"),
        "year_built": rec.get("year"),
        "estimated_value": rec.get("estimate_value"),
        "equity_percent": rec.get("equity_percent"),
        "last_sold": rec.get("last_sold"),
        "rental_value": rec.get("rental_value"),
        "tags": [t.get("name") if isinstance(t, dict) else str(t) for t in (rec.get("tags") or [])],
    }
    return {k: v for k, v in ctx.items() if v not in (None, "", [])}


def resolve_preset(title: str) -> tuple[Optional[dict], str]:
    """Find a filter preset by title and return its live `filters.must`.

    Pulling the cohort FROM the preset means we inherit every suppression rule
    already encoded there (dead statuses, Mail Only, Recently Sold, skiptraced,
    call-attempt gates). A record Ty edits in DataSift drops out of our sends
    automatically, with no second list to keep in sync.
    """
    c = client()
    if not c:
        return None, ""
    resp = c._request("/api/internal/filter-preset/", params={"limit": 999})
    presets = resp.get("results") or resp.get("data") or resp or []
    if isinstance(presets, dict):
        presets = presets.get("results") or []
    wanted = title.strip().lower()
    match = next((p for p in presets if (p.get("title") or "").strip().lower() == wanted), None)
    if match is None:
        match = next((p for p in presets if wanted in (p.get("title") or "").lower()), None)
    if match is None:
        return None, ""
    filters = match.get("filters") or match.get("filter_data") or {}
    must = filters.get("must") if isinstance(filters, dict) else None
    return (must if isinstance(must, dict) else None), match.get("title") or title


def fetch_cohort(must: dict, limit: int = 0) -> list[dict]:
    """Page the records currently sitting in a preset."""
    c = client()
    if not c:
        return []
    seen, out, offset = set(), [], 0
    while True:
        body = {"limit": 250, "offset": offset, "query": {"must": must}}
        resp = c._request("/api/internal/property/", method="POST", body=body,
                          method_override="GET")
        rows = resp.get("results") or resp.get("data") or []
        count = resp.get("count", 0)
        for row in rows:
            uuid = row.get("uuid")
            if uuid and uuid not in seen:
                seen.add(uuid)
                out.append(row)
        offset += len(rows)
        if not rows or offset >= count or (limit and len(out) >= limit):
            break
    return out[:limit] if limit else out


def list_presets() -> list[str]:
    c = client()
    if not c:
        return []
    resp = c._request("/api/internal/filter-preset/", params={"limit": 999})
    presets = resp.get("results") or resp.get("data") or []
    return sorted((p.get("title") or "") for p in presets if isinstance(p, dict))


def find_records_by_phone(phone: str, limit: int = 10) -> list[dict]:
    """Every record carrying this phone number, verified.

    The generic `search` filter matches a phone, but it is a fuzzy search, so
    each hit is confirmed against the owner's actual phone list before it counts.

    Returns a LIST on purpose. Skip-trace routinely attaches one number to
    several owners: 865-254-5044 came back on three records across two
    different owners. When somebody says "wrong number, leave me alone", that
    number is bad on every record it touches, not just the one we happened to
    text. Dispositioning only one leaves the others live to be dialled tomorrow.
    """
    c = client()
    digits = store.clean_phone(phone)
    if not c or len(digits) != 10:
        return []
    try:
        resp = c._request(
            "/api/internal/property/", method="POST",
            body={"limit": limit, "query": {"must": {"search": digits}}},
            method_override="GET",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("phone lookup failed for %s: %s", digits, exc)
        return []

    out = []
    for row in resp.get("results") or []:
        uuid = row.get("uuid")
        if not uuid:
            continue
        full = get_record(uuid) or {}
        owner = full.get("owner") if isinstance(full.get("owner"), dict) else {}
        for p in owner.get("phones") or []:
            if store.clean_phone(p.get("number")) == digits:
                out.append({
                    "record_uuid": uuid,
                    "owner_uuid": owner.get("uuid") or "",
                    "phone_obj": p,
                    "address": (full.get("address") or {}).get("street") or "",
                })
                break
    return out


def record_phones(record_uuid: str) -> list[str]:
    """Every phone on the record, cleaned to 10 digits.

    This exists because of what the June backfill showed: **5 of 9 real replies
    arrived from a DIFFERENT number than the one we texted.** People answer from
    whichever line is in their hand. Mapping only the number we sent to would
    leave the majority of replies unroutable, escalating them as "unknown"
    instead of attaching them to their record.
    """
    rec = get_record(record_uuid)
    if not rec:
        return []
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    out = []
    for phone in owner.get("phones") or []:
        digits = store.clean_phone(phone.get("number") if isinstance(phone, dict) else phone)
        status = (phone.get("status") or "").upper() if isinstance(phone, dict) else ""
        # A number already marked WRONG or DEAD still belongs to this record for
        # routing purposes: if it texts us, we want the context, not a mystery.
        if len(digits) == 10:
            out.append(digits)
    return out


def map_all_phones(record_uuid: str, context: Optional[dict] = None) -> int:
    """Map every phone on a record to it, so a reply from any line routes."""
    phones = record_phones(record_uuid)
    if not phones:
        return 0
    ctx = context if context is not None else deal_context(record_uuid)
    for phone in phones:
        store.map_phone(phone, record_uuid=record_uuid, context=ctx or None)
    return len(phones)


def owner_uuid_for(record_uuid: str) -> str:
    rec = get_record(record_uuid)
    owner = (rec or {}).get("owner") or {}
    return owner.get("uuid") or ""


def find_phone_object(record_uuid: str, phone: str) -> tuple[str, Optional[dict]]:
    """(owner_uuid, phone object) for a number on a record."""
    rec = get_record(record_uuid)
    if not rec:
        return "", None
    owner = rec.get("owner") if isinstance(rec.get("owner"), dict) else {}
    target = store.clean_phone(phone)
    for p in owner.get("phones") or []:
        if store.clean_phone(p.get("number")) == target:
            return owner.get("uuid") or "", p
    return owner.get("uuid") or "", None


# ------------------------------------------------------------------ writes

# Observed live on ty+2, not guessed. The DNC variants matter: Sift records
# "do not contact" as part of the phone status rather than as a separate flag,
# and it keeps the correct/wrong judgement alongside it. That makes the phone
# record itself the suppression, so a number stops being texted no matter which
# tool builds the next campaign.
PHONE_STATUSES = (
    "UNKNOWN", "CORRECT", "WRONG", "DEAD", "NO_ANSWER",
    "DNC", "CORRECT_DNC", "WRONG_DNC",
)

# What a phone becomes when its owner opts out, preserving what we already knew
# about whether the number reaches the right person.
DNC_FOR = {
    "CORRECT": "CORRECT_DNC",
    "WRONG": "WRONG_DNC",
    "WRONG_DNC": "WRONG_DNC",
    "CORRECT_DNC": "CORRECT_DNC",
}


def dnc_status(record_uuid: str, phone: str, wrong_number: bool = False) -> str:
    """The DNC status to write, given what the phone already says."""
    if wrong_number:
        return "WRONG_DNC"
    _, obj = find_phone_object(record_uuid, phone)
    current = ((obj or {}).get("status") or "UNKNOWN").upper()
    return DNC_FOR.get(current, "DNC")


DIAL_TIERS = ("Dial First", "Dial Second", "Dial Third", "Dial Fourth", "Drop")


def dial_tier_uuids(wanted=("Dial First", "Dial Second"), sample: int = 400) -> dict:
    """Tag NAME -> uuid for the dial tiers, derived once and cached.

    Two payloads describe the same phone differently: the slim search result
    carries tag UUIDs, the full record carries tag names. Names are the stable
    contract across accounts, but reading them costs one record fetch per
    candidate, which is fine for a hundred records and absurd for nine thousand.

    So the mapping is learned once by joining the two payloads on a handful of
    records, cached on disk, and everything afterwards filters on uuid straight
    from the search result at no extra cost. Cheap and still not hard-coded to
    one account's ids.
    """
    import json

    cached = store.get_meta("dial_tier_uuids")
    if cached:
        try:
            found = json.loads(cached)
            if all(w in found for w in wanted):
                return found
        except ValueError:
            pass

    c = client()
    if not c:
        return {}
    try:
        res = c._request(
            "/api/internal/property/", method="POST", method_override="GET",
            body={"limit": sample, "query": {"must": {"phone": 1}}},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not sample records to learn dial tier uuids: %s", exc)
        return {}

    found: dict = {}
    for rec in (res.get("results") or []):
        ph = rec.get("phone") or {}
        uuids = ph.get("tags") or []
        if not uuids:
            continue
        full = get_record(rec.get("uuid") or "")
        if not full:
            continue
        for p in ((full.get("owner") or {}).get("phones") or []):
            if store.clean_phone(p.get("number")) != store.clean_phone(ph.get("number")):
                continue
            names = [
                (t.get("title") or t.get("name")) if isinstance(t, dict) else str(t)
                for t in (p.get("tags") or [])
            ]
            if len(names) == len(uuids):
                for name, uu in zip(names, uuids):
                    if name in DIAL_TIERS:
                        found[name] = uu
            break
        if all(w in found for w in wanted):
            break

    if found:
        store.set_meta("dial_tier_uuids", json.dumps(found))
        log.info("learned dial tier uuids: %s", sorted(found))
    return found


def dial_tier_checked(record_uuid: str, phone: str) -> tuple[str, bool]:
    """(tier, whether the record could actually be read).

    The second value exists so a CRM outage is never silently reported as
    "these numbers are all low tier". One means send nothing until it is fixed;
    the other means the list is genuinely unqualified.
    """
    rec = get_record(record_uuid)
    if not rec:
        return "", False
    return dial_tier(record_uuid, phone), True


def dial_tier(record_uuid: str, phone: str) -> str:
    """Which dial tier a number carries, by tag NAME.

    Deliberately name-based. The slim search payload exposes phone tags as
    uuids, which are per-account values that would silently match nothing if
    this ever ran against a different Sift account; the tag names are the
    stable contract. The full record is lru-cached, so this costs one fetch per
    record rather than one per phone.

    Returns "" when the phone carries no tier tag at all.
    """
    _, obj = find_phone_object(record_uuid, phone)
    if not obj:
        return ""
    names = {
        (t.get("title") or t.get("name") or t.get("tag")) if isinstance(t, dict) else str(t)
        for t in (obj.get("tags") or [])
    }
    for tier in DIAL_TIERS:
        if tier in names:
            return tier
    return ""


def set_phone_status(record_uuid: str, phone: str, status: str) -> dict:
    """Flip one phone to any status in PHONE_STATUSES.

    Endpoint captured from the reisift UI: POST /api/internal/owner/{owner_uuid}
    /upsert-phones/ with the FULL phone object. It upserts by number, so the
    type / tags / is_connected must be preserved or they are wiped.
    """
    status = status.upper()
    if status not in PHONE_STATUSES:
        return {"error": f"invalid phone status {status}"}
    c = client()
    if not c:
        return {"error": unavailable_reason()}

    owner_uuid, phone_obj = find_phone_object(record_uuid, phone)
    if not owner_uuid or not phone_obj:
        return {"error": "owner_uuid or phone not found on record"}
    if (phone_obj.get("status") or "UNKNOWN").upper() == status:
        return {"skipped": "already " + status}

    tags = [
        t.get("title") or t.get("name") or t.get("tag") if isinstance(t, dict) else str(t)
        for t in (phone_obj.get("tags") or [])
    ]
    payload = {
        "number": store.clean_phone(phone_obj.get("number")),
        "type": phone_obj.get("type") or "UNKNOWN",
        "tags": [t for t in tags if t],
        "status": status,
        "is_connected": phone_obj.get("is_connected", True),
        "verified": phone_obj.get("verified", False),
    }
    if config.DRY_RUN:
        return _dry("set_phone_status", owner=owner_uuid, payload=payload)
    try:
        c._request(
            f"/api/internal/owner/{owner_uuid}/upsert-phones/",
            method="POST",
            body={"phones": [payload]},
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"write failed: {exc}"}

    # Read back. A write that 200s and changes nothing is the failure mode
    # this whole codebase keeps rediscovering.
    _, after = find_phone_object(record_uuid, phone)
    got = (after or {}).get("status", "")
    return {"written": status, "verified": (got or "").upper() == status}


def add_tags(record_uuid: str, tags: list[str]) -> dict:
    c = client()
    if not c:
        return {"error": unavailable_reason()}
    if config.DRY_RUN:
        return _dry("add_tags", uuid=record_uuid, tags=tags)
    try:
        return c.add_tags(record_uuid, tags, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def set_status(record_uuid: str, status: str) -> dict:
    c = client()
    if not c:
        return {"error": unavailable_reason()}
    if config.DRY_RUN:
        return _dry("set_status", uuid=record_uuid, status=status)
    try:
        return c.update_status(record_uuid, status, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def assign(record_uuid: str, user_uuid: str) -> dict:
    """Set the record's assignee.

    Deliberately NOT crm_api.assign(): that method PATCHes `assignee`, which
    returns 200 and silently no-ops. The field the API actually reads is
    `assigned_to` (a bare uuid, not a dict).
    """
    c = client()
    if not c:
        return {"error": unavailable_reason()}
    if config.DRY_RUN:
        return _dry("assign", uuid=record_uuid, assigned_to=user_uuid)
    try:
        return c._request(
            f"/api/internal/property/{record_uuid}/",
            method="PATCH",
            body={"assigned_to": user_uuid},
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def bump_sms_attempts(record_uuid: str) -> dict:
    """Add one to the record's SMS attempt counter in Sift.

    Verified writable and persistent on `property.sms_attempts` (2026-08-12).

    Worth doing for a reason beyond bookkeeping: the counter is the only place a
    HUMAN sees that the agent has been working a record. A prospector opening a
    file otherwise has no idea whether it has had one text or three, which is
    exactly the context that decides how they open the call. It also makes
    "texts sent" answerable from Sift itself rather than only from our SQLite,
    so the number survives this box.

    Read-modify-write, which can lose a concurrent increment. Accepted: sends
    are paced a minute apart and one record is never texted twice in a day, so
    two writers on one record is not a situation that arises.
    """
    c = client()
    if not c:
        return {"error": unavailable_reason()}
    if config.DRY_RUN:
        return _dry("bump_sms_attempts", uuid=record_uuid)
    try:
        current = c._request(f"/api/internal/property/{record_uuid}/", method="GET")
        count = int(current.get("sms_attempts") or 0) + 1
        c._request(
            f"/api/internal/property/{record_uuid}/", method="PATCH",
            body={"sms_attempts": count},
        )
        return {"sms_attempts": count}
    except Exception as exc:  # noqa: BLE001 - never fail a send over a counter
        return {"error": str(exc)}


def tag_uuids(names) -> list:
    """Resolve tag titles to uuids. Property queries filter on uuid, not title."""
    c = client()
    if not c:
        return []
    try:
        raw = c._request("/api/internal/tag/?limit=1000", method="GET")
    except Exception as exc:  # noqa: BLE001
        log.warning("tag lookup failed: %s", exc)
        return []
    rows = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
    lookup = {t.get("title"): t.get("uuid") for t in rows if t.get("title")}
    return [lookup[n] for n in names if n in lookup]


def reassert_handoff_assignment(assignee_uuid: str, limit: int = 200) -> dict:
    """Re-pin every escalated record onto the handoff owner.

    Escalation sets status to new_lead, and a DataSift sequence on that status
    change reassigns the record to whoever owns new leads. That sequence runs
    asynchronously, so it can land AFTER our assignment and quietly take the
    record back. The prospector then cannot open the hot lead she was just
    paged about, which is the exact failure this whole handoff exists to avoid.

    Winning that race is not something we control, so instead we keep checking.
    The query is one call against a handful of records, cheap enough to run
    every worker pass.
    """
    if not assignee_uuid:
        return {"skipped": "no handoff assignee configured"}
    c = client()
    if not c:
        return {"error": unavailable_reason()}

    want = tag_uuids([config.TAG_ESCALATED])
    if not want:
        return {"checked": 0, "reassigned": 0}
    try:
        res = c._request(
            "/api/internal/property/", method="POST", method_override="GET",
            body={"limit": limit, "query": {"must": {"any_tags": want}}},
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    rows = res.get("results") or res.get("data") or []
    moved = []
    for row in rows:
        uuid = row.get("uuid")
        if not uuid:
            continue
        full = get_record(uuid) or {}
        current = full.get("assigned_to")
        if isinstance(current, dict):
            current = current.get("uuid") or ""
        if current == assignee_uuid:
            continue
        result = assign(uuid, assignee_uuid)
        if not result.get("error"):
            moved.append(uuid)
            log.info("re-pinned escalated record %s (was %s)", uuid[:8], current or "unassigned")
    return {"checked": len(rows), "reassigned": len(moved)}


def post_note(record_uuid: str, message: str, pinned: bool = False) -> dict:
    c = client()
    if not c:
        return {"error": unavailable_reason()}
    if config.DRY_RUN:
        return _dry("post_note", uuid=record_uuid, message=message[:120])
    try:
        return c.add_message(record_uuid, message, pinned=pinned, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
