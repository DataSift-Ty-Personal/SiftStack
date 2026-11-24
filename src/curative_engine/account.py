"""The user's own account: every record on the lists and tags that mark a death or an estate, read in full over
the DataSift Open API key, with each record's SiftMap detail (allowed: the houses are already records).

Nothing is written to DataSift here, with one opt-in exception: `widen` pulls county houses SiftMap flags for a
death or an estate into the account (quoted first, and only with --commit and an approved count).

What is kept on disk (work/records.json, work/details.json) is what the ranking reads: the owner's phones and
emails are dropped from the saved copy (only how many there are is kept).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

from . import common as C

if str(C.SRC) not in sys.path:
    sys.path.insert(0, str(C.SRC))

DEFAULT_LISTS = ("Obituary", "Probate", "Foreclosure")
WALL = 10000                         # the records search refuses offset + limit past 10,000
DEATH_FLAGS = ("is_heirs_property", "is_inheritance_property", "is_pre_probate", "is_probate_properties",
               "is_obituary_property", "is_estate_sales")
WIDEN_LIST = "Curative Candidates"
WIDEN_TAG = "Curative Candidate"


# ----------------------------------------------------------------------------------- clients
def crm_client(gap: float | None = None):
    """The autopilot's DataSift client on the Open API key and nothing else (no password, no login token)."""
    import datasift_api_upload as up
    from agents.autopilot.crm import CRM
    key = (up.env().get("REISIFT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("REISIFT_API_KEY is not set: put your DataSift Open API key in .env at the repo root "
                         "(DataSift: Settings, then API). This tool uses the key and nothing else.")
    pinned = os.environ.get("AUTOPILOT_API_GAP_S")
    return CRM(api=up.Api(api_key=key), min_gap=float(pinned) if pinned else gap)


def siftmap_client():
    import datasift_api_upload as up
    from siftmap_standalone import SiftMapClient
    key = (up.env().get("REISIFT_API_KEY") or "").strip()
    return SiftMapClient(api_key=key) if key else None


# ----------------------------------------------------------------------------------- the search
def scan(crm, must: dict, wall: int = WALL, start: str = "2000-01-01", end: str | None = None) -> list:
    """Every search row matching `must`, past the 10,000-row wall: a window that holds more is cut in two by
    the day each record was created (the one date the search honors for this, proven 2026-11-12)."""
    n = crm.count(must)
    if n <= wall:
        return crm.rows(must, cap=wall)
    end = end or (C.today() + timedelta(days=1)).isoformat()
    a, b = C.parse_day(start), C.parse_day(end)
    if not a or not b or (b - a).days <= 1:
        return crm.rows(dict(must, created=[start, end]), cap=wall)       # one day over the wall: take what fits
    mid = (a + (b - a) / 2).isoformat()
    out = []
    for lo, hi in ((start, mid), (mid, end)):
        m = dict(must, created=[lo, hi])
        k = crm.count(m)
        if not k:
            continue
        out += crm.rows(m, cap=wall) if k <= wall else scan(crm, must, wall, lo, hi)
    seen, uniq = set(), []
    for r in out:
        if r.get("uuid") and r["uuid"] not in seen:
            seen.add(r["uuid"])
            uniq.append(r)
    return uniq


def field_options(crm) -> dict:
    """option uuid -> label for every select custom field (a select's value is its option's uuid)."""
    from agents.autopilot.crm import rows_of
    out = {}
    for f in rows_of(crm.call("/api/internal/custom-fields/?limit=999")):
        for o in f.get("options") or []:
            if isinstance(o, dict) and o.get("uuid"):
                out[str(o["uuid"])] = o.get("label") or o.get("title") or o.get("value") or ""
    return out


def slim_record(rec: dict) -> dict:
    """The record as the ranking reads it: phones and emails become counts."""
    r = dict(rec or {})
    owner = dict(r.get("owner") or {})
    phones = owner.pop("phones", None) or []
    emails = owner.pop("emails", None) or []
    owner["phone_count"] = len(phones) if isinstance(phones, list) else 0
    owner["email_count"] = len(emails) if isinstance(emails, list) else 0
    owner["phone_statuses"] = sorted({str((p or {}).get("status") or "UNKNOWN") for p in phones if isinstance(p, dict)})
    r["owner"] = owner
    for k in ("phones", "emails"):
        r.pop(k, None)
    return r


def county_key(name) -> str:
    return str(name or "").strip().lower().replace(" county", "").split(",")[0].strip()


def state_ok(row: dict, states: set[str]) -> bool:
    """A search row carries the state but no county: rows in another state are never read in full."""
    if not states:
        return True
    st = C.state_code((row.get("address") or {}).get("state"))
    return not st or st in states


def in_county(rec: dict, counties: set[str]) -> bool:
    """The full record carries its county ("Knox"); no counties named means every county."""
    if not counties:
        return True
    return county_key((rec.get("address") or {}).get("county")) in counties


def pull(ws, crm, sm=None, *, lists=DEFAULT_LISTS, tags=(), counties=(), states=(), fresh_days: int = 7,
         max_records: int = 0, out=print, sleep=time.sleep) -> dict:
    """Read the candidate records and their SiftMap detail into the work folder. Resumable: a record read in
    the last `fresh_days` is not read again; a run stopped part way picks up where it stopped."""
    want_counties = {county_key(c) for c in counties if c}
    want_states = {C.state_code(s) for s in states if C.state_code(s)}
    found: dict = {}
    missing = []
    optional = {WIDEN_LIST.lower()}            # the list `widen` fills: read when it exists, never reported missing
    lists = list(lists) + [t for t in (WIDEN_LIST,) if t.lower() not in {x.lower() for x in lists}]
    for title in lists:
        u = crm.list_uuid(title)
        if not u:
            if title.lower() not in optional:
                missing.append("list %s" % title)
            continue
        for r in scan(crm, {"any_lists": [u]}):
            if state_ok(r, want_states):
                found.setdefault(r["uuid"], {"row": r, "via": []})["via"].append("list " + title)
    for title in tags:
        u = crm.tag_uuid(title)
        if not u:
            missing.append("tag %s" % title)
            continue
        for r in scan(crm, {"any_tags": [u]}):
            if state_ok(r, want_states):
                found.setdefault(r["uuid"], {"row": r, "via": []})["via"].append("tag " + title)
    out("%d records on %s%s" % (len(found), ", ".join(list(lists) + list(tags)),
                                (" in %s" % ", ".join(sorted(want_states))) if want_states else ""))
    for m in missing:
        out("  not in this account: %s" % m)
    records = ws.read("records.json", {}) or {}
    opts = ws.read("field_options.json")
    if opts is None:
        opts = field_options(crm)
        ws.write("field_options.json", opts)
    cutoff = (C.today() - timedelta(days=max(0, int(fresh_days)))).isoformat()
    todo_all = [u for u in found if (records.get(u) or {}).get("read", "") < cutoff]
    todo = todo_all[:int(max_records)] if max_records else todo_all
    left = len(todo_all) - len(todo)
    out("reading %d record(s) in full (%d already fresh%s)" % (
        len(todo), len(found) - len(todo_all), (", %d left for the next run" % left) if left else ""))
    for i, u in enumerate(todo, 1):
        try:
            rec = crm.record(u) or {}
            custom = crm.custom_fields(u) or {}
        except Exception as exc:  # noqa: BLE001  (one unreadable record is noted; the run goes on)
            records[u] = {"error": str(exc)[:160], "read": "", "via": found[u]["via"]}
            continue
        custom = {k: opts.get(str(v), v) if isinstance(v, str) else v for k, v in custom.items()}
        records[u] = {"record": slim_record(rec), "custom": custom, "via": found[u]["via"], "read": C.today().isoformat()}
        if i % 25 == 0:
            ws.write("records.json", records, indent=None)
            out("  %d/%d" % (i, len(todo)))
    for u in found:
        if u in records:
            records[u]["via"] = found[u]["via"]
    # the county is on the full record only: a record outside the counties named, or not read yet, is left out of
    # the ranking (it stays in the file); a record no longer on any list or tag drops out the same way
    scope = sorted(u for u in found if (records.get(u) or {}).get("record")
                   and in_county(records[u]["record"], want_counties))
    ws.write("records.json", records, indent=None)
    ws.write("scope.json", {"uuids": scope, "lists": list(lists), "tags": list(tags),
                            "counties": sorted(want_counties), "states": sorted(want_states), "read": C.today().isoformat()})
    out("in scope: %d record(s)%s" % (len(scope), (" in %s" % ", ".join(sorted(want_counties))) if want_counties else ""))
    summary = {"found": len(found), "in_scope": len(scope), "read": len(todo), "left": left, "missing": missing,
               "details": 0}
    if sm is not None:
        summary["details"] = pull_details(ws, sm, [records[u] for u in scope],
                                          fresh_days=max(fresh_days, 30), out=out, sleep=sleep)
    return summary


def pull_details(ws, sm, recs: list, fresh_days: int = 30, out=print, sleep=time.sleep) -> int:
    """SiftMap's detail for each record that carries a SiftMap id (dataflik_id). These are houses already in the
    account, which the acceptable use policy allows; nothing outside the account is read in volume here."""
    details = ws.read("details.json", {}) or {}
    cutoff = (C.today() - timedelta(days=fresh_days)).isoformat()
    ids = []
    for x in recs:
        df = str(((x or {}).get("record") or {}).get("dataflik_id") or "")
        if df and (details.get(df) or {}).get("_read", "") < cutoff:
            ids.append(df)
    ids = list(dict.fromkeys(ids))
    out("reading SiftMap detail for %d house(s)" % len(ids))
    n = 0
    for i, df in enumerate(ids, 1):
        try:
            d = sm.get_detail(df) or {}
        except Exception as exc:  # noqa: BLE001
            details[df] = {"_error": str(exc)[:160], "_read": ""}
            continue
        d.pop("parcel_boundary", None)                 # a polygon: large, and never read
        oi = d.get("owner_info") or {}
        if isinstance(oi, dict):
            oi.pop("owner_properties", None)
        d["_read"] = C.today().isoformat()
        details[df] = d
        n += 1
        if i % 25 == 0:
            ws.write("details.json", details, indent=None)
    ws.write("details.json", details, indent=None)
    return n


# ----------------------------------------------------------------------------------- widen
def widen(ws, sm, crm, county, *, approve: int = 0, commit: bool = False, list_name: str = WIDEN_LIST,
          tag: str = WIDEN_TAG, max_ids: int = 2000, out=print, wait_min: float = 10.0) -> dict:
    """County houses SiftMap flags for a death or an estate (heirs, inheritance, pre-probate, probate,
    obituary, estate sale) that are NOT records yet. Dry: the count and the documented quote of the allowance
    they would use. With --commit and an approved count that covers the quote: they are pulled onto the list
    `Curative Candidates`, one first and read back, then the rest. A pull spends record allowance, and the
    account may skip trace new records on its own."""
    import d4d_call_list as d4d
    from dpd_live import county_address, WORLD
    from agents.workflows import list_pull as lp
    if not county.fips:
        raise SystemExit("widen needs a county SiftMap knows by FIPS (for example --county 47093 or 'Knox, TN')")
    c = {"fips": county.fips, "name": county.name, "state": county.state}
    ids, rows_seen = [], 0
    page = 250
    filters = {"type_single_family": True, "distressors_include": ",".join(DEATH_FLAGS),
               "distressors_include_min_match": 1}
    offset = 0
    while len(ids) < max_ids and offset < WALL:
        body = {"result_index": 1 + offset, "with_boundaries": False, "filters": filters,
                "addresses": [county_address(c)], "polygon": WORLD, "size": page}
        r = sm.search(body) or {}
        rows = r.get("data") or r.get("results") or []
        rows_seen += len(rows)
        for h in rows:
            if not h.get("saved") and h.get("id"):
                ids.append(str(h["id"]))
        if len(rows) < page:
            break
        offset += page
    ids = list(dict.fromkeys(ids))[:max_ids]
    ws.write("widen_ids.json", {"county": county.label, "ids": ids, "read": C.today().isoformat()})
    out("%s: %d house(s) with a death or estate flag read, %d not in the account yet" % (county.label, rows_seen, len(ids)))
    if not ids:
        return {"ids": 0}
    q = lp.quote(sm, ids, list_name, tag)
    out("quote: %d new record(s) would use the allowance (billable %d, allowance left %s)"
        % (q.get("new", 0), q.get("billable", 0), q.get("allowance_left")))
    if not commit:
        out("DRY: nothing pulled. To pull them: --commit --approve %d" % q.get("billable", 0))
        return {"ids": len(ids), "quote": q}
    why = lp.refusal(q, int(approve))
    if why:
        raise SystemExit("refused: " + why)
    new_ids = [i for i in ids]
    first, why = lp.probe(crm, sm, new_ids[0], list_name, tag, wait_min)
    if why:
        raise SystemExit("stopped on the first house: " + why)
    for i in range(1, len(new_ids), lp.CHUNK):
        sm._request(lp.ADD, "POST", d4d.add_query_body(new_ids[i:i + lp.CHUNK], list_name, tag))
    landed = d4d.poll_saved(sm, new_ids, wait_min=wait_min, every=30.0)
    out("pulled: %d of %d landed on %s (run `rank` again to read them)" % (len(landed), len(new_ids), list_name))
    return {"ids": len(ids), "quote": q, "landed": len(landed), "first": first}
