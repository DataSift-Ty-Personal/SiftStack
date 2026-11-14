"""Uploads that used to drive the browser wizard, over the API instead (build plan 5.5).

Two flows still opened a browser to put data into DataSift: the courthouse photo import (the
Dropbox watcher) and the trace merges (Tracerfy and Enformion numbers written back onto existing
records). A wizard works while a person watches it and hangs at 4am: a modal steals focus, a
column fails to map, and the run stalls instead of failing. Everything those flows write has a
documented route, so they move here.

    create a record          POST /api/internal/property/        (datasift_api_upload.create_property)
    lists, tags, fields      the same calls the daily uploader makes
    phones                   POST /api/internal/owner/{uuid}/upsert-phones/
    "this owner was traced"  POST /api/internal/owner/{uuid}/skiptrace-attempts/   (the TOTAL)

THE FIRST RECORD IS READ BACK BEFORE THE REST GO. That one habit caught, on earlier uploads, a tag
sent as one comma string, an owner the API refused, a select option sent by label, and a list
name that would have attached nothing for 2,512 of 2,573 records. Here it is not a habit, it is
the code: the first record's lists, tags and phones are read off the account, and anything
missing STOPS the upload with the reason.

RULES THAT COME WITH PHONES, each from a number that went wrong once:
  * upsert-phones replaces a phone object BY NUMBER. A number the owner already has is never
    sent, so a status or a tier a person set by hand is never overwritten.
  * A merge never CREATES a record. A traced address that is not in the account is counted and
    left out; creating it would upload a record with no list and no reason to exist.
  * When numbers land from outside DataSift the owner's skip trace counter goes up by one, or
    the record reads "never skip traced" and falls out of every Ready to Call preset.

WHAT THE API CANNOT DO YET. The wizard's post-upload "Enrich Property Information" has no scoped
route (the only one enriches the whole account), so a record uploaded here keeps the property
facts DataSift fills in on its own. The wizard's skip trace is replaced by DataSift's automatic
trace of new records, or by the scoped trace stage.

    FTM_UPLOAD_BROWSER=1     use the browser wizard as before (the fallback)
"""
from __future__ import annotations

import os
import re
import time

_TRUE = ("1", "true", "yes", "on")
PHONE_COLUMNS = [("Phone 1", "UNKNOWN"), ("Phone 2", "MOBILE"), ("Phone 3", "MOBILE"), ("Phone 4", "MOBILE"),
                 ("Phone 5", "MOBILE"), ("Phone 6", "MOBILE"), ("Phone 7", "LANDLINE"), ("Phone 8", "LANDLINE"),
                 ("Phone 9", "LANDLINE")]


def browser_wanted() -> bool:
    """True when the operator asked for the old wizard path."""
    return (os.environ.get("FTM_UPLOAD_BROWSER") or "").strip().lower() in _TRUE


def clean_number(raw) -> str:
    """Ten digits, or "" when it is not a US number."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def phones_of(row: dict) -> list[tuple[str, str]]:
    """(number, line type) for every phone column on an upload row, each number once."""
    out, seen = [], set()
    for column, kind in PHONE_COLUMNS:
        number = clean_number(row.get(column))
        if number and number not in seen:
            seen.add(number)
            out.append((number, kind))
    return out


def _owner(rec: dict) -> dict:
    owner = rec.get("owner")
    return owner if isinstance(owner, dict) else {}


def new_phones(rec: dict, wanted: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The wanted numbers the record's owner does not already carry."""
    have = {clean_number(p.get("number")) for p in (_owner(rec).get("phones") or []) if isinstance(p, dict)}
    return [(n, k) for n, k in wanted if n not in have]


def write_phones(api, rec: dict, wanted: list[tuple[str, str]], source_tag: str = "") -> int:
    """Add the numbers the owner lacks and count the trace. Returns how many numbers were sent."""
    owner_uuid = _owner(rec).get("uuid")
    fresh = new_phones(rec, wanted)
    if not owner_uuid or not fresh:
        return 0
    tags = [source_tag] if source_tag else []
    api.call("/api/internal/owner/%s/upsert-phones/" % owner_uuid, "POST", {"phones": [
        {"number": n, "type": kind, "tags": tags, "status": "UNKNOWN", "is_connected": True, "verified": False}
        for n, kind in fresh]})
    total = int(_owner(rec).get("skiptrace_attempts") or 0) + 1
    api.call("/api/internal/owner/%s/skiptrace-attempts/" % owner_uuid, "POST", {"skiptrace_attempts": total})
    return len(fresh)


def read_back(api, uuid: str, row: dict, sent_phones: list[str]) -> str:
    """"" when the record carries what was sent, else the first thing that is missing."""
    import datasift_api_upload as up

    rec = api.call("/api/internal/property/%s/" % uuid)
    lists = {x.lower() for x in up._names(rec.get("lists"))}
    for want in [x.strip() for x in str(row.get("Lists") or "").split(",") if x.strip()]:
        if want.lower() not in lists:
            return "the record is not on the list %r" % want
    tags = {x.lower() for x in up._names(rec.get("tags"))}
    for want in [x.strip() for x in str(row.get("Tags") or "").split(",") if x.strip()]:
        if want.lower() not in tags:
            return "the record does not carry the tag %r" % want
    have = {clean_number(p.get("number")) for p in (_owner(rec).get("phones") or []) if isinstance(p, dict)}
    for number in sent_phones:
        if number not in have:
            return "a phone number that was sent is not on the owner"
    return ""


def upload_rows(rows: list[dict], *, commit: bool = False, api=None, source_tag: str = "",
                verify_first: bool = True, sleep: float = 0.2) -> dict:
    """Create or attach each row, with its fields, notes and phones. Stops when the first record
    does not read back. Returns counts; `stopped` is the reason when it stopped."""
    import datasift_api_upload as up

    stats = {"submitted": len(rows), "created": 0, "existing": 0, "fielded": 0, "phones": 0, "failed": 0,
             "warnings": [], "stopped": "", "committed": commit}
    if not commit or not rows:
        return stats
    api = api or up.Api()
    idx = up.field_index(api)
    warn: set = set()
    for i, row in enumerate(rows):
        try:
            res = up.create_property(api, up.build_property(row))
            uuid = res.get("uuid") if isinstance(res, dict) else None
            if not uuid:
                raise RuntimeError("no record id came back")
            stats["created"] += 1
            if res.get("existing"):
                stats["existing"] += 1
            pairs = up.field_pairs(row, idx, warn)
            if pairs and up.write_fields(api, uuid, pairs, warn):
                stats["fielded"] += 1
            if row.get("Notes"):
                api.call("/api/internal/property/%s/add-notes/" % uuid, "POST", {"notes": str(row["Notes"])[:2000]})
            wanted = phones_of(row)
            sent: list[str] = []
            if wanted:
                rec = api.call("/api/internal/property/%s/" % uuid)
                sent = [n for n, _k in new_phones(rec, wanted)]
                stats["phones"] += write_phones(api, rec, wanted, source_tag)
            if verify_first and i == 0:
                bad = read_back(api, uuid, row, sent)
                if bad:
                    stats["stopped"] = "the first record did not read back: %s. Nothing else was sent." % bad
                    stats["failed"] += len(rows) - 1
                    break
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            if len(warn) < 20:
                warn.add("%s: %s" % (str(row.get("Property Street Address") or "")[:30], str(exc)[:120]))
        time.sleep(sleep)
    stats["warnings"] = sorted(warn)
    return stats


def rows_from_notices(notices: list) -> list[dict]:
    """Upload rows for NoticeData records, exactly as the pipeline's own file would carry them."""
    from datasift_formatter import _build_row
    return [_build_row(n) for n in notices]


def upload_notices(notices: list, *, commit: bool = False, api=None, source_tag: str = "") -> dict:
    """The courthouse photo import's upload."""
    return upload_rows(rows_from_notices(notices), commit=commit, api=api, source_tag=source_tag)


def merge_phones(rows: list[dict], *, commit: bool = False, api=None, source_tag: str = "",
                 list_name: str = "", tags: list[str] | None = None, sleep: float = 0.2) -> dict:
    """Write traced numbers onto records that ALREADY exist, found by address.

    Never creates a record. The list and tags are attached to the record the numbers land on, the
    way the wizard's "add data to an existing list" did. The first record is read back before
    the rest are written."""
    import datasift_api_upload as up

    stats = {"submitted": len(rows), "matched": 0, "not_in_account": 0, "no_new_numbers": 0, "phones": 0,
             "failed": 0, "warnings": [], "stopped": "", "committed": commit}
    if not commit or not rows:
        return stats
    api = api or up.Api()
    first = True
    for row in rows:
        wanted = phones_of(row)
        if not wanted:
            continue
        try:
            uuid = up.find_property(api, {"street": row.get("Property Street Address") or "",
                                          "postal_code": row.get("Property ZIP Code") or ""})
            if not uuid:
                stats["not_in_account"] += 1
                continue
            stats["matched"] += 1
            rec = api.call("/api/internal/property/%s/" % uuid)
            sent = [n for n, _k in new_phones(rec, wanted)]
            if not sent:
                stats["no_new_numbers"] += 1
                continue
            stats["phones"] += write_phones(api, rec, wanted, source_tag)
            body = {}
            if list_name:
                body["lists"] = [list_name]
            if tags:
                body["tags"] = list(tags)
            if body:
                up.attach_to_record(api, uuid, body)
            if first:
                first = False
                check = {"Lists": list_name, "Tags": ",".join(tags or [])}
                bad = read_back(api, uuid, check, sent)
                if bad:
                    stats["stopped"] = "the first record did not read back: %s. Nothing else was sent." % bad
                    break
        except Exception as exc:  # noqa: BLE001
            stats["failed"] += 1
            if len(stats["warnings"]) < 20:
                stats["warnings"].append("%s: %s" % (str(row.get("Property Street Address") or "")[:30], str(exc)[:120]))
        time.sleep(sleep)
    return stats
