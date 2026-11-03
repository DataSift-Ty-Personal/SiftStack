"""D4D Top N call list: DataSift skip trace + Trestle dial tiers + dialer CSV.

Takes the top N rows of the Driving for Dollars visual distress ranking
(`Knox_Blount_Master_<date>.xlsx`, sheet "All properties", already sorted by the
vendor's visual_distress_score), makes sure every one of them is a DataSift
record, skip traces the newcomers through DataSift's own unlimited plan, scores
every unscored phone through Trestle, writes the dial tier back onto the CRM
phone, and exports two CSVs a dialer can load as-is.

The workbook rows are keyed by SiftMap `PropertyID`, which IS the CRM record's
`dataflik_id` (verified 80/80 on a live sample), so the join is exact.

Phases, in order. DRY by default; anything that writes or spends needs --commit.

    python src/d4d_call_list.py --phase cohort              # top N + live CRM membership
    python src/d4d_call_list.py --phase pull --commit       # pull the missing ones into the CRM
    python src/d4d_call_list.py --phase skiptrace --commit  # list-scoped DataSift skip trace (browser)
    python src/d4d_call_list.py --phase harvest             # read every record's phones
    python src/d4d_call_list.py --phase score --commit      # Trestle, $0.015 per unscored number
    python src/d4d_call_list.py --phase writeback --commit  # dial tiers onto the CRM phones
    python src/d4d_call_list.py --phase export              # the CSVs
    python src/d4d_call_list.py --phase dialer              # dialer upload CSV, Dial First/Second only
    python src/d4d_call_list.py --phase kixie               # Kixie PowerList CSV, one row per property
    python src/d4d_call_list.py --phase qa                  # counts, cost, read-backs

Contracts this leans on (all verified live before this was written):
  * SiftMap `POST /properties/search/` with `addresses: [{"search": "<id>",
    "type": "ids"}]` returns the search row for exactly those ids, including
    `saved` and `saved_uuid`, so CRM membership is read live rather than off a
    week-old sweep file.
  * `POST /api/internal/property/skip-trace/` IGNORES scoping and traces the
    whole account, so the skip trace here goes through the list-scoped browser
    wizard (`datasift_uploader.skip_trace_records`).
  * `POST /api/internal/owner/{uuid}/upsert-phones/` upserts BY NUMBER and
    replaces the object, so type / tags / status / is_connected are carried
    through, and any other Dial tier is stripped before the new one is added.
  * `add-lists` takes a STRING title; an array 201s and does nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import Api, ApiError, attach_to_record, env  # noqa: E402
import phone_validator as pv  # noqa: E402

_API_CLIENTS = Path(r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients")

OUT = Path("output/d4d_call_list")
STATE_PATH = OUT / "state.json"
COHORT_PATH = OUT / "cohort.json"
RECORDS_PATH = OUT / "records.json"
SCORES_PATH = OUT / "phone_scores.json"
ATTACH_PATH = OUT / "attach_state.json"
WRITEBACK_PATH = OUT / "writeback_state.json"

DEFAULT_MASTER = "output/Knox_Blount_Master_20260908.xlsx"
ASSIGNED_JSONL = Path("output/tn_market/assigned.jsonl")
DEFAULT_LIST = "D4D Top 3000"
DEFAULT_TAG = "D4D"
CRM_URL = "https://app.reisift.io/records/%s"

# A number the team already disposed is not re-bought and not re-dialed.
BAD_STATUSES = {"WRONG", "DEAD", "DNC", "CORRECT_DNC", "WRONG_DNC"}
TIER_NAMES = set(pv.DEFAULT_TIERS)
TIER_ORDER = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2,
              "Dial Fourth": 3, "": 4, "Drop": 5}
TYPE_ORDER = {"MOBILE": 0, "LANDLINE": 1, "UNKNOWN": 2}
LITIGATOR_TAG = "DNC"
PHONE_SLOTS = 5
_RETRY_HINT = re.compile(r"available in ([0-9]+)", re.I)


# ----------------------------------------------------------------- state --

def _load(p: Path, default):
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def state() -> dict:
    return _load(STATE_PATH, {})


def mark_done(phase: str, **facts) -> None:
    s = state()
    s[phase] = {"when": datetime.now().isoformat(timespec="seconds"), **facts}
    _save(STATE_PATH, s)


def require(phase: str) -> None:
    if phase not in state():
        raise SystemExit("phase '%s' has not run yet; run it first" % phase)


def gate(ok: bool, msg: str) -> None:
    """A phase that produced nothing is a failure, not an empty market."""
    if not ok:
        raise SystemExit("GATE FAILED: " + msg)


# ------------------------------------------------------------- api plumbing --

_last_call = [0.0]


def _call(api: Api, path: str, method: str = "GET", body=None, headers=None,
          tries: int = 6, min_gap: float = 0.45):
    """`Api.call` with the throttle backoff every /api/internal read needs.

    reisift 429s hard and answers with its own "available in N seconds" hint;
    it also 502s in bursts. Neither is an answer. Writes ARE retried here on a
    429 only (nothing was applied), never on a 5xx.
    """
    last = None
    for attempt in range(tries):
        gap = time.time() - _last_call[0]
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last_call[0] = time.time()
        try:
            return api.call(path, method, body, headers=headers)
        except ApiError as e:
            last = e
            if e.code == 429:
                hit = _RETRY_HINT.search(e.body or "")
                wait = float(int(hit.group(1)) + 1) if hit else 3.0 * (attempt + 1)
                time.sleep(min(wait, 120))
                continue
            if e.code in (500, 502, 503, 504) and method == "GET":
                time.sleep(2.0 ** attempt)
                continue
            raise
        except (OSError, TimeoutError) as e:  # network blips
            last = e
            time.sleep(2.0 ** attempt)
    raise RuntimeError("gave up on %s %s: %s" % (method, path, last))


def _siftmap():
    from siftmap_standalone import SiftMapClient
    key = env().get("REISIFT_API_KEY", "")
    gate(bool(key), "REISIFT_API_KEY is not set; SiftMap search needs it")
    return SiftMapClient(key, min_interval=0.5)


def search_by_ids(m, ids: list) -> list:
    """SiftMap search rows for exactly these dataflik_ids (live `saved_uuid`)."""
    import siftmap_pull as sp
    body = {"result_index": 1, "with_boundaries": False, "filters": {},
            "addresses": [{"search": str(i), "type": "ids"} for i in ids],
            "polygon": sp.WORLD}
    r = m._request("/properties/search/", method="POST", body=body)
    return r.get("data") or []


def tag_names(p: dict) -> list:
    out = []
    for t in (p.get("tags") or []):
        if isinstance(t, dict):
            t = t.get("title") or t.get("name") or t.get("tag")
        if t:
            out.append(str(t))
    return out


def phone_tier(p: dict) -> str:
    for t in tag_names(p):
        if t in TIER_NAMES:
            return t
    return ""


def fmt_phone(digits: str) -> str:
    d = pv.clean_phone(digits)
    if len(d) != 10:
        return digits or ""
    return "(%s) %s-%s" % (d[:3], d[3:6], d[6:])


def no_dashes(s) -> str:
    """Never ship an em or en dash. Plain hyphen instead."""
    return str(s if s is not None else "").replace("\u2014", "-").replace("\u2013", "-")


# ----------------------------------------------------------------- cohort --

def read_master(path: str, top: int) -> list:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["All properties"]
    rows = ws.iter_rows(values_only=True)
    hdr = [str(h) for h in next(rows)]
    out = []
    for r in rows:
        d = dict(zip(hdr, r))
        if d.get("Status") != "scored":
            break  # scored rows come first, sorted descending
        out.append({
            "rank": len(out) + 1,
            "score": d.get("Score"),
            "dataflik_id": str(d.get("PropertyID")),
            "address": d.get("Address") or "",
            "zip": str(d.get("ZIP") or ""),
            "neighborhood": d.get("Neighborhood") or "",
            "county": d.get("County") or "",
            "est_value": d.get("Est value"),
            "equity_pct": d.get("Equity %"),
            "owner_label": d.get("Owner") or "",
            "beds": d.get("Beds"), "baths": d.get("Baths"), "sqft": d.get("Sqft"),
            "known_distress": d.get("Known distress") or "",
        })
        if len(out) >= top:
            break
    wb.close()
    return out


def phase_cohort(args) -> None:
    rows = read_master(args.master, args.top)
    gate(len(rows) == args.top, "expected %d scored rows, read %d" % (args.top, len(rows)))
    print("cohort: %d rows, score %s -> %s" % (len(rows), rows[0]["score"], rows[-1]["score"]))
    print("  county: %s" % dict(Counter(r["county"] for r in rows)))
    print("  owner label: %s" % dict(Counter(r["owner_label"] for r in rows)))

    m = _siftmap()
    by_id = {r["dataflik_id"]: r for r in rows}
    ids = list(by_id)
    found = 0
    for i in range(0, len(ids), args.chunk):
        chunk = ids[i:i + args.chunk]
        hits = search_by_ids(m, chunk)
        if len(hits) < len(chunk):
            print("  chunk %d: %d of %d ids returned" % (i // args.chunk + 1, len(hits), len(chunk)))
        for h in hits:
            r = by_id.get(str(h.get("id")))
            if not r:
                continue
            found += 1
            r["saved_uuid"] = h.get("saved_uuid") or ""
            r["saved"] = bool(h.get("saved"))
            r["absentee"] = h.get("absenteeOwner")
            r["corporate"] = h.get("corporateOwned")
            r["lat"], r["lon"] = h.get("latitude"), h.get("longitude")
            r["siftmap_address"] = h.get("address") or ""
    missing = [r for r in rows if "saved" not in r]
    if missing and ASSIGNED_JSONL.exists():
        # The sweep file is a week old but still names the record for anything
        # the live search skipped.
        want = {r["dataflik_id"] for r in missing}
        with ASSIGNED_JSONL.open(encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                i = str(d.get("id"))
                if i in want:
                    r = by_id[i]
                    r["saved_uuid"] = d.get("saved_uuid") or ""
                    r["saved"] = bool(r["saved_uuid"])
                    r["from_sweep_file"] = True
    for r in rows:
        r.setdefault("saved_uuid", "")
        r.setdefault("saved", False)
    n_saved = sum(1 for r in rows if r["saved_uuid"])
    print("  live search matched %d; in CRM %d; to pull %d" % (found, n_saved, len(rows) - n_saved))
    _save(COHORT_PATH, rows)
    mark_done("cohort", rows=len(rows), in_crm=n_saved, to_pull=len(rows) - n_saved,
              score_lo=rows[-1]["score"], score_hi=rows[0]["score"], master=args.master)


# ------------------------------------------------------------------- pull --

def _ensure_title(api: Api, kind: str, title: str, commit: bool) -> str:
    """uuid of the list/tag with this title, creating it when --commit."""
    r = _call(api, "/api/internal/%s/?limit=999" % kind)
    rows = r.get("results") if isinstance(r, dict) else r
    for row in rows or []:
        if (row.get("title") or "").strip().casefold() == title.strip().casefold():
            return row.get("uuid") or ""
    print("  %s %r does not exist%s" % (kind, title, "" if commit else " (dry run: would create)"))
    if not commit:
        return "DRY-RUN-UUID"
    return _call(api, "/api/internal/%s/" % kind, "POST", {"title": title}).get("uuid") or ""


def _map_post(api: Api, path: str, body: dict):
    import siftmap_pull as sp
    return sp.Map(api)._post(path, body)


def add_by_ids(api: Api, ids: list, list_name: str, tag: str, route: str):
    """Pull dataflik_ids into the account. Route A = add-properties-by-query
    with an ids-typed address list (the verified search shape); route B = the
    documented-only /properties/add-properties/ body."""
    import siftmap_pull as sp
    if route == "A":
        return _map_post(api, "/properties/add-properties-by-query/", {
            "auto_add_enabled": False, "lists": [list_name], "tags": [tag],
            "replace_owners": False,
            "query": {"result_index": 1, "with_boundaries": False, "filters": {},
                      "addresses": [{"search": str(i), "type": "ids"} for i in ids],
                      "polygon": sp.WORLD}})
    return _map_post(api, "/properties/add-properties/", {
        "ids": [str(i) for i in ids], "lists": [list_name], "tags": [tag],
        "replace_owners": False})


def poll_saved(m, ids: list, wait_min: float, every: float = 20.0) -> dict:
    """id -> saved_uuid for whichever of `ids` show saved before the deadline."""
    deadline = time.time() + wait_min * 60
    landed = {}
    pending = [str(i) for i in ids]
    while pending:
        for i in range(0, len(pending), 100):
            for h in search_by_ids(m, pending[i:i + 100]):
                if h.get("saved") and h.get("saved_uuid"):
                    landed[str(h.get("id"))] = h["saved_uuid"]
        pending = [i for i in pending if i not in landed]
        if not pending or time.time() > deadline:
            break
        print("    %d landed, %d pending; waiting %ss" % (len(landed), len(pending), int(every)))
        time.sleep(every)
    return landed


def phase_pull(args) -> None:
    require("cohort")
    rows = _load(COHORT_PATH, [])
    api = Api()
    import siftmap_pull as sp
    used, limit = sp.usage(api)
    missing = [r for r in rows if not r["saved_uuid"]]
    # Rows pulled by this run got the list and tag from the add itself.
    existing = [r for r in rows if r["saved_uuid"] and not r.get("pulled")]
    print("pull: %d missing, %d already in CRM; allowance %s / %s" % (len(missing), len(existing), used, limit))
    gate(used + len(missing) <= limit, "the pull would cross the record allowance")

    list_uuid = _ensure_title(api, "list", args.list_name, args.commit)
    tag_uuid = _ensure_title(api, "tag", args.tag, args.commit)
    if not args.commit:
        print("DRY RUN. Would probe one id, then pull %d, then attach %d existing records "
              "to %r + tag %r. Re-run with --commit." % (len(missing), len(existing), args.list_name, args.tag))
        return

    st = state().get("pull", {})
    route = st.get("route") or ""
    m = _siftmap()
    new_uuids = dict(st.get("new_uuids") or {})
    probe_id = None

    if missing and not route:
        probe_id = missing[0]["dataflik_id"]
        for candidate in ("A", "B"):
            print("  probe route %s with id %s" % (candidate, probe_id))
            try:
                resp = add_by_ids(api, [probe_id], args.list_name, args.tag, candidate)
                print("    response: %s" % str(resp)[:160])
            except Exception as e:  # noqa: BLE001
                print("    route %s failed: %s" % (candidate, str(e)[:160]))
                continue
            landed = poll_saved(m, [probe_id], wait_min=args.probe_wait)
            if landed:
                u = landed[probe_id]
                rec = _call(api, "/api/internal/property/%s/" % u)
                lists = [(l.get("title") if isinstance(l, dict) else l) for l in rec.get("lists") or []]
                ok = str(rec.get("dataflik_id")) == probe_id
                print("    read-back: uuid %s dataflik_id match %s lists %s" % (u, ok, lists))
                gate(ok, "probe landed but dataflik_id does not match the pulled id")
                if args.list_name not in lists:
                    attach_to_record(api, u, {"lists": [args.list_name], "tags": [args.tag]})
                    print("    list was not attached by the add; attached it directly")
                route = candidate
                new_uuids[probe_id] = u
                break
        gate(bool(route), "neither pull route landed the probe id %s; nothing batched" % probe_id)
        mark_done("pull", route=route, new_uuids=new_uuids, probe=probe_id, partial=True)

    todo = [r["dataflik_id"] for r in missing if r["dataflik_id"] not in new_uuids]
    print("  route %s; %d ids to pull in chunks of %d" % (route, len(todo), args.chunk_pull))
    for i in range(0, len(todo), args.chunk_pull):
        chunk = todo[i:i + args.chunk_pull]
        resp = add_by_ids(api, chunk, args.list_name, args.tag, route)
        print("  chunk %d: %d ids -> %s" % (i // args.chunk_pull + 1, len(chunk), str(resp)[:100]))
        time.sleep(2)
    if todo:
        landed = poll_saved(m, todo, wait_min=args.wait_min)
        new_uuids.update(landed)
        print("  landed %d of %d" % (len(landed), len(todo)))
        mark_done("pull", route=route, new_uuids=new_uuids, partial=len(landed) < len(todo))
    for r in rows:
        if r["dataflik_id"] in new_uuids:
            r["saved_uuid"] = new_uuids[r["dataflik_id"]]
            r["saved"] = True
            r["pulled"] = True
    _save(COHORT_PATH, rows)

    # Existing records: list + tag accumulate, owner untouched. Resumable.
    att = _load(ATTACH_PATH, {"done": []})
    done = set(att["done"])
    todo_att = [r for r in existing if r["saved_uuid"] not in done]
    print("  attaching %d existing records to %r (%d already done)" % (len(todo_att), args.list_name, len(done)))
    for n, r in enumerate(todo_att, 1):
        try:
            attach_to_record(api, r["saved_uuid"], {"lists": [args.list_name], "tags": [args.tag]})
            done.add(r["saved_uuid"])
        except Exception as e:  # noqa: BLE001
            print("    attach failed %s: %s" % (r["saved_uuid"], str(e)[:100]))
        if n % 50 == 0:
            att["done"] = sorted(done)
            _save(ATTACH_PATH, att)
            print("    attached %d/%d" % (n, len(todo_att)))
        time.sleep(0.3)
    att["done"] = sorted(done)
    _save(ATTACH_PATH, att)

    still_missing = sum(1 for r in rows if not r["saved_uuid"])
    mark_done("pull", route=route, new_uuids=new_uuids, pulled=len(new_uuids),
              attached=len(done), still_missing=still_missing,
              list_uuid=list_uuid, tag_uuid=tag_uuid, partial=still_missing > 0)
    print("pull: %d pulled, %d attached, %d still missing" % (len(new_uuids), len(done), still_missing))


# -------------------------------------------------------------- skiptrace --

async def _run_wizard(list_name: str, headless: bool) -> dict:
    from playwright.async_api import async_playwright
    from datasift_core import login
    from datasift_uploader import skip_trace_records
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        try:
            ok = await login(page)
            if not ok:
                return {"success": False, "message": "DataSift login failed"}
            return await skip_trace_records(page, list_name)
        finally:
            await browser.close()


def _phone_count(api: Api, uuid: str) -> int:
    rec = _call(api, "/api/internal/property/%s/" % uuid)
    return len(((rec.get("owner") or {}).get("phones")) or [])


def phase_skiptrace(args) -> None:
    require("pull")
    import datasift_skiptrace as ds
    api = Api()
    before = ds.stats(api)
    print("skip trace stats before: traced %s, value spent $%s, saved by unlimited $%s" % (
        before.get("total_properties"), before.get("value_spent"), before.get("saved_by_unlimited_skiptracing")))
    gate(float(before.get("value_spent") or 0) == 0, "value_spent is not zero; this is not the unlimited plan")
    new = state()["pull"].get("new_uuids") or {}
    sample = random.sample(sorted(new.values()), min(25, len(new))) if new else []
    if sample:
        pre = sum(1 for u in sample if _phone_count(api, u) > 0)
        print("  %d of %d sampled new records already carry phones" % (pre, len(sample)))
    if not args.commit:
        print("DRY RUN. Would run the list-scoped wizard on %r (headed=%s). Re-run with --commit."
              % (args.list_name, not args.headless))
        return
    res = asyncio.run(_run_wizard(args.list_name, args.headless))
    print("  wizard: %s" % res.get("message"))
    gate(bool(res.get("success")), "the skip trace wizard did not submit")
    got = 0
    if sample and not args.no_wait:
        deadline = time.time() + args.wait_min * 60
        while time.time() < deadline:
            got = sum(1 for u in sample if _phone_count(api, u) > 0)
            print("    %d of %d sampled new records have phones" % (got, len(sample)))
            if got >= 0.8 * len(sample):
                break
            time.sleep(60)
    after = ds.stats(api)
    mark_done("skiptrace", submitted=True, sample=len(sample), sample_with_phones=got,
              traced_before=before.get("total_properties"), traced_after=after.get("total_properties"),
              value_spent_after=after.get("value_spent"))
    print("skip trace: traced %s -> %s, value spent $%s" % (
        before.get("total_properties"), after.get("total_properties"), after.get("value_spent")))


# ---------------------------------------------------------------- harvest --

def _slim(rec: dict, dataflik_id: str, pulled: bool) -> dict:
    ow = rec.get("owner") or {}
    oa = ow.get("address") or {}
    a = rec.get("address") or {}
    return {
        "uuid": rec.get("uuid"), "dataflik_id": dataflik_id,
        "record_dataflik_id": rec.get("dataflik_id"), "pulled": pulled,
        "owner_uuid": ow.get("uuid"),
        "owner_first": ow.get("first_name") or "", "owner_last": ow.get("last_name") or "",
        "owner_company": ow.get("company") or "",
        "mailing_street": oa.get("street") or "", "mailing_city": oa.get("city") or "",
        "mailing_state": oa.get("state") or "", "mailing_zip": str(oa.get("postal_code") or oa.get("zip5") or ""),
        "street": a.get("street") or "", "city": a.get("city") or "", "state": a.get("state") or "",
        "zip": str(a.get("postal_code") or a.get("zip5") or ""), "county": a.get("county") or "",
        "estimate_value": rec.get("estimate_value"), "equity_percent": rec.get("equity_percent"),
        "status": rec.get("status") or "",
        "lists": [(l.get("title") if isinstance(l, dict) else l) for l in rec.get("lists") or []],
        "tags": tag_names(rec),
        "phones": ow.get("phones") or [],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def phase_harvest(args) -> None:
    require("cohort")
    rows = _load(COHORT_PATH, [])
    api = Api()
    recs = _load(RECORDS_PATH, {})
    todo = [r for r in rows if r["saved_uuid"]
            and (args.refresh or r["saved_uuid"] not in recs
                 or (args.refresh_pulled and r.get("pulled")))]
    print("harvest: %d records to read (%d cached, %d rows without a CRM record)" % (
        len(todo), len(recs), sum(1 for r in rows if not r["saved_uuid"])))
    failed = 0
    for n, r in enumerate(todo, 1):
        try:
            rec = _call(api, "/api/internal/property/%s/" % r["saved_uuid"])
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  read failed %s: %s" % (r["saved_uuid"], str(e)[:100]))
            continue
        recs[r["saved_uuid"]] = _slim(rec, r["dataflik_id"], bool(r.get("pulled")))
        recs[r["saved_uuid"]]["address_dedup"] = bool(r.get("address_dedup"))
        if n % 100 == 0:
            _save(RECORDS_PATH, recs)
            print("  %d/%d" % (n, len(todo)))
    _save(RECORDS_PATH, recs)
    have = [recs[r["saved_uuid"]] for r in rows if r["saved_uuid"] in recs]
    gate(len(have) > 0, "no records read")
    # An address dedup (SiftMap holds two ids for one address; DataSift kept its
    # own record) legitimately carries a different dataflik_id.
    mismatch = sum(1 for x in have if x["record_dataflik_id"] and not x.get("address_dedup")
                   and str(x["record_dataflik_id"]) != x["dataflik_id"])
    n_phones = [len(x["phones"]) for x in have]
    zero = sum(1 for k in n_phones if k == 0)
    tiered = sum(1 for x in have for p in x["phones"] if phone_tier(p))
    print("harvest: %d records, %d failed, %d dataflik mismatches, %d phones total, %d already tiered, %d records with no phone" % (
        len(have), failed, mismatch, sum(n_phones), tiered, zero))
    gate(mismatch == 0, "%d records carry a dataflik_id that is not the cohort id" % mismatch)
    mark_done("harvest", records=len(have), failed=failed, phones=sum(n_phones),
              already_tiered=tiered, no_phone=zero)


# ------------------------------------------------------------------ score --

def is_eligible(p: dict) -> bool:
    """Unscored, not disposed, and a real 10-digit number."""
    if phone_tier(p) or LITIGATOR_TAG in tag_names(p):
        return False
    if (p.get("status") or "UNKNOWN").upper() in BAD_STATUSES:
        return False
    return len(pv.clean_phone(p.get("number") or "")) == 10


def eligible_numbers(recs: dict) -> dict:
    """number -> [(record uuid, phone obj)] for every phone worth scoring."""
    out = defaultdict(list)
    for u, x in recs.items():
        for p in x["phones"]:
            if is_eligible(p):
                out[pv.clean_phone(p["number"])].append((u, p))
    return out


def _router():
    try:
        sys.path.insert(0, str(_API_CLIENTS))
        from trestle_router import TrestleRouter, resolve_keys
        ks = resolve_keys()
        gate(bool(ks["free"] or ks["paid"]), "no Trestle key in .env")
        return TrestleRouter(free_key=ks["free"], paid_key=ks["paid"],
                             free_limit=ks["free_limit"], free_reset=ks["free_reset"])
    except ImportError:
        return None


def _score_one(router, key: str, num: str) -> dict:
    if router is not None:
        res = router.lookup(num, add_litigator=True)
        data = res.get("data") if res.get("ok") else None
        if data is None:
            return {"error": res.get("error") or ("HTTP %s" % res.get("status_code")), "key_used": res.get("key_used")}
        key_used = res.get("key_used")
    else:
        data = pv.call_trestle(num, key, add_litigator=True)
        key_used = "paid"
        if data.get("error"):
            return {"error": data["error"], "key_used": key_used}
    score = data.get("activity_score")
    lit = None
    add = data.get("add_ons") or {}
    if isinstance(add, dict) and isinstance(add.get("litigator_checks"), dict):
        lit = add["litigator_checks"].get("phone.is_litigator_risk")
    tier = pv.assign_tier(score, pv.DEFAULT_TIERS)
    if lit:
        tier = LITIGATOR_TAG
    return {"score": score, "tier": tier, "line_type": data.get("line_type"),
            "carrier": data.get("carrier"), "is_valid": data.get("is_valid"),
            "litigator": bool(lit), "key_used": key_used,
            "when": datetime.now().isoformat(timespec="seconds")}


def phase_score(args) -> None:
    require("harvest")
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    elig = eligible_numbers(recs)
    unscored = [n for n in elig if not (cache.get(n) or {}).get("tier")]
    print("score: %d eligible numbers across %d records, %d cached, %d to score, est $%.2f" % (
        len(elig), len(recs), len(elig) - len(unscored), len(unscored), len(unscored) * pv.COST_PER_PHONE))
    if not args.commit:
        print("DRY RUN. Re-run with --commit to spend.")
        return
    if args.limit:
        unscored = unscored[:args.limit]
    router = _router()
    key = os.getenv("TRESTLE_PAID_API_KEY") or env().get("TRESTLE_PAID_API_KEY") or env().get("TRESTLE_API_KEY")
    print("  routing: %s" % ("trestle_router (free first)" if router else "phone_validator.call_trestle"))
    done = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(_score_one, router, key, n): n for n in unscored}
        for f in as_completed(futs):
            n = futs[f]
            try:
                cache[n] = f.result()
            except Exception as e:  # noqa: BLE001
                cache[n] = {"error": str(e)[:120]}
            if cache[n].get("error"):
                errors += 1
            done += 1
            if done % 50 == 0:
                _save(SCORES_PATH, cache)
                print("  scored %d/%d (%d errors)" % (done, len(unscored), errors))
    _save(SCORES_PATH, cache)
    if router is not None:
        router.flush()
        print("  trestle usage: %s" % json.dumps(router.usage()))
    by_tier = Counter((cache.get(n) or {}).get("tier") or "error" for n in elig)
    print("score: %d scored this run, %d errors; tiers %s" % (done, errors, dict(by_tier)))
    gate(errors < max(1, len(unscored) // 5), "more than 20%% of scoring calls errored (%d)" % errors)
    mark_done("score", eligible=len(elig), scored_now=done, errors=errors, by_tier=dict(by_tier),
              usage=router.usage() if router is not None else None)


# -------------------------------------------------------------- writeback --

def payload_for_tier(p: dict, tier: str) -> dict:
    """The FULL phone object upsert-phones needs, with exactly one tier on it.

    Strip any other Dial tier first: a union leaves a re-scored phone carrying
    two tiers, matching two dialer presets at once.
    """
    tags = [t for t in tag_names(p) if t not in TIER_NAMES]
    if tier and tier not in tags:
        tags.append(tier)
    return {"number": pv.clean_phone(p.get("number") or "") or p.get("number"),
            "type": p.get("type") or "UNKNOWN",
            "tags": sorted(tags),
            "status": (p.get("status") or "UNKNOWN").upper(),
            "is_connected": p.get("is_connected", True),
            "verified": p.get("verified", False)}


def planned_writes(recs: dict, cache: dict) -> dict:
    """owner_uuid -> {"record": uuid, "phones": [payload...], "expect": {num: tier}}"""
    out = {}
    for u, x in recs.items():
        if not x.get("owner_uuid"):
            continue
        changed, expect = [], {}
        for p in x["phones"]:
            if not is_eligible(p):
                continue
            num = pv.clean_phone(p["number"])
            tier = (cache.get(num) or {}).get("tier")
            if not tier or tier == "Unknown":
                continue
            changed.append(payload_for_tier(p, tier))
            expect[num] = tier
        if changed:
            out[x["owner_uuid"]] = {"record": u, "phones": changed, "expect": expect}
    return out


def phase_writeback(args) -> None:
    require("score")
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    plan = planned_writes(recs, cache)
    n_phones = sum(len(v["phones"]) for v in plan.values())
    wb = _load(WRITEBACK_PATH, {"done": []})
    done = set(wb["done"])
    todo = {o: v for o, v in plan.items() if o not in done}
    print("writeback: %d owners / %d phones planned, %d owners already written" % (len(plan), n_phones, len(done)))
    if not args.commit:
        print("DRY RUN. Re-run with --commit.")
        return
    api = Api()
    failed = 0
    for n, (owner, v) in enumerate(todo.items(), 1):
        try:
            _call(api, "/api/internal/owner/%s/upsert-phones/" % owner, "POST", {"phones": v["phones"]})
            done.add(owner)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  write failed owner %s: %s" % (owner, str(e)[:120]))
        if n % 50 == 0:
            wb["done"] = sorted(done)
            _save(WRITEBACK_PATH, wb)
            print("  %d/%d" % (n, len(todo)))
    wb["done"] = sorted(done)
    _save(WRITEBACK_PATH, wb)

    # Read back: every scored phone carries exactly the tier we wrote, no other.
    # Owners whose record was refreshed on an earlier read-back are no longer
    # in the plan (their phones now carry the tier), so sample the overlap.
    verifiable = sorted(set(done) & set(plan))
    check = random.sample(verifiable, min(args.readback, len(verifiable)))
    bad = 0
    for owner in check:
        v = plan[owner]
        rec = _call(api, "/api/internal/property/%s/" % v["record"])
        phones = {pv.clean_phone(p.get("number") or ""): p for p in ((rec.get("owner") or {}).get("phones") or [])}
        for num, tier in v["expect"].items():
            p = phones.get(num)
            got = [t for t in tag_names(p or {}) if t in TIER_NAMES or t == LITIGATOR_TAG]
            if not p or got != [tier]:
                bad += 1
                print("  MISMATCH %s %s expected [%s] got %s" % (v["record"], num, tier, got))
        # refresh the local copy so export reflects the CRM
        recs[v["record"]]["phones"] = (rec.get("owner") or {}).get("phones") or []
    _save(RECORDS_PATH, recs)
    print("writeback: %d owners written, %d failed, read-back %d owners, %d mismatches" % (
        len(done), failed, len(check), bad))
    mark_done("writeback", owners=len(done), phones=n_phones, failed=failed,
              readback=len(check), mismatches=bad)
    gate(bad == 0, "%d read-back mismatches" % bad)


# ----------------------------------------------------------------- export --

def sort_phones(phones: list, cache: dict) -> list:
    """Callable phones best-first: tier, then mobile before landline, then the
    Trestle score. Disposed numbers and litigators are dropped entirely."""
    out = []
    for p in phones:
        num = pv.clean_phone(p.get("number") or "")
        if len(num) != 10:
            continue
        if (p.get("status") or "UNKNOWN").upper() in BAD_STATUSES:
            continue
        tags = tag_names(p)
        if LITIGATOR_TAG in tags:
            continue
        info = cache.get(num) or {}
        if info.get("litigator"):
            continue
        tier = phone_tier(p) or (info.get("tier") if info.get("tier") not in (None, "Unknown", LITIGATOR_TAG) else "")
        out.append({"phone": num, "phone_display": fmt_phone(num),
                    "type": (p.get("type") or "UNKNOWN").upper(), "tier": tier or "",
                    "trestle_score": info.get("score"),
                    "status": (p.get("status") or "UNKNOWN").upper()})
    out.sort(key=lambda q: (TIER_ORDER.get(q["tier"], 4), TYPE_ORDER.get(q["type"], 2),
                            -(q["trestle_score"] if isinstance(q["trestle_score"], (int, float)) else -1)))
    return out


def owner_type(x: dict, label: str) -> str:
    if x.get("owner_company") and not (x.get("owner_first") or x.get("owner_last")):
        return "Company"
    return label or ""


def build_rows(cohort: list, recs: dict, cache: dict) -> tuple:
    """(call list rows, phone rows, no-phone rows)."""
    call_rows, phone_rows, none_rows = [], [], []
    seen_uuid = {}
    for r in cohort:
        x = recs.get(r["saved_uuid"]) if r.get("saved_uuid") else None
        base = {"rank": r["rank"], "d4d_score": r["score"],
                "address": r["address"].split(",")[0].strip() if not x else x["street"],
                "city": x["city"] if x else "", "state": x["state"] if x else "TN",
                "zip": str(x["zip"] if x and x["zip"] else r["zip"])[:5],
                "county": r["county"], "neighborhood": r["neighborhood"],
                "crm_record_url": CRM_URL % r["saved_uuid"] if r.get("saved_uuid") else "",
                "property_id": r["dataflik_id"]}
        if not x:
            none_rows.append({**base, "reason": "no CRM record"})
            continue
        if r["saved_uuid"] in seen_uuid:
            # The ranking carries two SiftMap ids for one address; one CRM record.
            none_rows.append({**base, "reason": "duplicate of rank %d (same address, second SiftMap id)"
                              % seen_uuid[r["saved_uuid"]]})
            continue
        seen_uuid[r["saved_uuid"]] = r["rank"]
        phones = sort_phones(x["phones"], cache)
        name = " ".join(t for t in (x["owner_first"], x["owner_last"]) if t) or x["owner_company"]
        row = {**base,
               "owner_first": x["owner_first"], "owner_last": x["owner_last"],
               "owner_company": x["owner_company"],
               "mailing_address": x["mailing_street"], "mailing_city": x["mailing_city"],
               "mailing_state": x["mailing_state"], "mailing_zip": x["mailing_zip"],
               "owner_type": owner_type(x, r.get("owner_label", "")),
               "est_value": x.get("estimate_value") if x.get("estimate_value") is not None else r.get("est_value"),
               "equity_pct": x.get("equity_percent") if x.get("equity_percent") is not None else r.get("equity_pct"),
               "known_distress": r["known_distress"], "lead_status": x["status"],
               "source": "new" if x.get("pulled") else "existing"}
        for i in range(PHONE_SLOTS):
            q = phones[i] if i < len(phones) else {}
            k = "phone_%d" % (i + 1)
            row[k] = q.get("phone", "")
            row[k + "_display"] = q.get("phone_display", "")
            row[k + "_type"] = q.get("type", "")
            row[k + "_tier"] = q.get("tier", "")
            row[k + "_score"] = q.get("trestle_score", "")
        row["more_phones"] = max(0, len(phones) - PHONE_SLOTS)
        row["callable_phones"] = len(phones)
        if not phones:
            none_rows.append({**base, "reason": "no callable phone (%d on record)" % len(x["phones"])})
        call_rows.append(row)
        for q in phones:
            phone_rows.append({"rank": r["rank"], "d4d_score": r["score"], "owner_name": name,
                               "address": base["address"], "city": base["city"], "zip": base["zip"],
                               **q, "source": row["source"],
                               "crm_record_url": base["crm_record_url"], "property_id": r["dataflik_id"]})
    return call_rows, phone_rows, none_rows


CALL_FIELDS = (["rank", "d4d_score", "address", "city", "state", "zip", "county", "neighborhood",
                "owner_first", "owner_last", "owner_company", "mailing_address", "mailing_city",
                "mailing_state", "mailing_zip", "owner_type", "est_value", "equity_pct",
                "known_distress", "lead_status"]
               + [f for i in range(1, PHONE_SLOTS + 1)
                  for f in ("phone_%d" % i, "phone_%d_display" % i, "phone_%d_type" % i,
                            "phone_%d_tier" % i, "phone_%d_score" % i)]
               + ["more_phones", "callable_phones", "source", "crm_record_url", "property_id"])
PHONE_FIELDS = ["rank", "d4d_score", "owner_name", "address", "city", "zip", "phone", "phone_display",
                "type", "tier", "trestle_score", "status", "source", "crm_record_url", "property_id"]
NONE_FIELDS = ["rank", "d4d_score", "address", "city", "state", "zip", "county", "neighborhood",
               "reason", "crm_record_url", "property_id"]


def write_csv(path: Path, rows: list, fields: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: no_dashes(r.get(k, "")) for k in fields})


def phase_export(args) -> None:
    require("harvest")
    cohort = _load(COHORT_PATH, [])
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    call_rows, phone_rows, none_rows = build_rows(cohort, recs, cache)
    stamp = datetime.now().strftime("%Y%m%d")
    stem = "D4D_Top%d" % len(cohort)
    p1 = OUT / ("%s_CallList_%s.csv" % (stem, stamp))
    p2 = OUT / ("%s_Phones_%s.csv" % (stem, stamp))
    p3 = OUT / ("%s_no_phone_%s.csv" % (stem, stamp))
    write_csv(p1, call_rows, CALL_FIELDS)
    write_csv(p2, phone_rows, PHONE_FIELDS)
    write_csv(p3, none_rows, NONE_FIELDS)
    for p in (p1, p2, p3):
        txt = p.read_text(encoding="utf-8")
        gate("\u2014" not in txt and "\u2013" not in txt, "dash leaked into %s" % p.name)
    tiers = Counter(q["tier"] or "unscored" for q in phone_rows)
    with_phone = sum(1 for r in call_rows if r["callable_phones"])
    print("export: %d properties (%d with a callable phone), %d phone rows, %d without a phone" % (
        len(call_rows), with_phone, len(phone_rows), len(none_rows)))
    print("  phone tiers: %s" % dict(tiers))
    print("  %s\n  %s\n  %s" % (p1, p2, p3))
    mark_done("export", properties=len(call_rows), with_phone=with_phone, phones=len(phone_rows),
              no_phone=len(none_rows), tiers=dict(tiers), files=[str(p1), str(p2), str(p3)])



# ----------------------------------------------------------------- dialer --

DIALER_BASE = ["First Name", "Last Name", "Property address", "Property city", "Property state",
               "Property zip", "Property county"]


def dialer_rows(cohort: list, recs: dict, cache: dict, tiers: set, phone_cols: int,
                suppress_lists: set = frozenset()) -> tuple:
    """One row per property in the dialer upload layout, only phones in `tiers`,
    best first. Properties with no qualifying number are dropped and counted;
    so is any record on a suppressed list (Obituary by default: a decedent home
    is a research case, not a bulk touch)."""
    call_rows, _, _ = build_rows(cohort, recs, cache)
    out, dropped, truncated, suppressed = [], 0, 0, 0
    sup = {t.casefold() for t in suppress_lists}
    by_uuid = {}
    for r in cohort:
        if r.get("saved_uuid") and r["saved_uuid"] not in by_uuid:
            by_uuid[r["saved_uuid"]] = r
    for row in call_rows:
        x = recs.get(row["crm_record_url"].rsplit("/", 1)[-1]) or {}
        if any((l or "").strip().casefold() in sup for l in x.get("lists") or []):
            suppressed += 1
            continue
        phones = [q["phone"] for q in sort_phones(x.get("phones") or [], cache) if q["tier"] in tiers]
        if not phones:
            dropped += 1
            continue
        if len(phones) > phone_cols:
            truncated += 1
        first, last = row["owner_first"], row["owner_last"]
        if not (first or last) and row["owner_company"]:
            last = row["owner_company"]
        d = {"First Name": first, "Last Name": last, "Property address": row["address"],
             "Property city": row["city"], "Property state": row["state"] or "TN",
             "Property zip": row["zip"], "Property county": row["county"]}
        for i in range(phone_cols):
            d["Phone %d" % (i + 1)] = phones[i] if i < len(phones) else ""
        out.append(d)
    return out, dropped, truncated, suppressed


def phase_dialer(args) -> None:
    require("harvest")
    cohort = _load(COHORT_PATH, [])
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    suppress = {t.strip() for t in args.suppress_lists.split(",") if t.strip()}
    rows, dropped, truncated, suppressed = dialer_rows(cohort, recs, cache, tiers, args.phone_cols, suppress)
    fields = DIALER_BASE + ["Phone %d" % (i + 1) for i in range(args.phone_cols)]
    p = OUT / ("D4D_Top%d_Dialer_%s.csv" % (len(cohort), datetime.now().strftime("%Y%m%d")))
    write_csv(p, rows, fields)
    n_phones = sum(1 for r in rows for i in range(args.phone_cols) if r["Phone %d" % (i + 1)])
    print("dialer: %d properties, %d numbers (%s), %d suppressed (on %s), %d dropped with no qualifying "
          "number, %d had more than %d numbers (extra dropped)"
          % (len(rows), n_phones, ", ".join(sorted(tiers)), suppressed, ", ".join(sorted(suppress)) or "nothing",
             dropped, truncated, args.phone_cols))
    print("  %s" % p)
    mark_done("dialer", properties=len(rows), phones=n_phones, dropped=dropped, truncated=truncated,
              suppressed=suppressed, suppress_lists=sorted(suppress),
              tiers=sorted(tiers), file=str(p))


# ------------------------------------------------------------------ kixie --
# Kixie PowerList contract (support.kixie.com, read 2026-09-15). ONE ROW PER
# CONTACT: "if a spreadsheet has multiple phone numbers for a Contact within a
# single row, each phone number in that Contact's row will be grouped" into a
# Contact Group (up to 5 numbers), and the agent's outcome actions (Remove This
# Number, Always Use This Number, Remove This Contact) act on that group. One
# row per number would make the same owner five unrelated contacts. Standard
# fields: Phone / Email / First Name / Last Name / Title / Company Name /
# PowerList Notes (Next Call Refresher), plus up to SIX custom fields, all shown
# on connect. Phone "for best results" in E.164 without the plus (18005551234).

KIXIE_PHONES = 5
KIXIE_FIELDS = (["Phone %d" % (i + 1) for i in range(KIXIE_PHONES)]
                + ["First Name", "Last Name", "Company Name", "Title", "PowerList Notes",
                   "Property Address", "Property City", "Property State", "Property Zip",
                   "Dial Tiers", "CRM Record"])


def _targets(cohort: list, recs: dict, cache: dict, tiers: set, suppress_lists) -> tuple:
    """(property row, [qualifying phones best first]) per kept property, plus
    the no-number and suppressed counts. Shared by the dialer and Kixie exports."""
    call_rows, _, _ = build_rows(cohort, recs, cache)
    sup = {t.casefold() for t in suppress_lists}
    out, dropped, suppressed = [], 0, 0
    for row in call_rows:
        x = recs.get(row["crm_record_url"].rsplit("/", 1)[-1]) or {}
        if any((l or "").strip().casefold() in sup for l in x.get("lists") or []):
            suppressed += 1
            continue
        phones = [q for q in sort_phones(x.get("phones") or [], cache) if q["tier"] in tiers]
        if not phones:
            dropped += 1
            continue
        out.append((row, phones))
    return out, dropped, suppressed


def kixie_rows(cohort: list, recs: dict, cache: dict, tiers: set, suppress_lists=frozenset(),
               phone_cols: int = KIXIE_PHONES, dedupe: bool = True) -> tuple:
    """One Kixie row per property (a Contact Group), Phone 1..N best first.

    A number is dialed ONCE: the same owner holds several of these houses, so
    with `dedupe` each number stays only on its highest-ranked property and a
    row that loses every number is folded into the surviving row's notes
    ("also owns ..."), instead of the dialer ringing one person per house.
    """
    kept, dropped, suppressed = _targets(cohort, recs, cache, tiers, suppress_lists)
    out, truncated, folded = [], 0, 0
    seen: dict = {}        # number -> index in out (the row that keeps it)
    also: dict = {}        # index in out -> [addresses folded into it]
    for row, phones in kept:
        if dedupe:
            fresh = [q for q in phones if q["phone"] not in seen]
            if not fresh:
                folded += 1
                owner_idx = seen[phones[0]["phone"]]
                also.setdefault(owner_idx, []).append(row["address"])
                continue
            phones = fresh
        if len(phones) > phone_cols:
            truncated += 1
        use = phones[:phone_cols]
        for q in use:
            seen.setdefault(q["phone"], len(out))
        first, last = row["owner_first"], row["owner_last"]
        company = row["owner_company"]
        if not (first or last) and company:
            last = company
        tier_list = ", ".join(q["tier"].replace("Dial ", "") for q in use)
        note = "D4D score %s | %d number%s: %s | %s, %s %s %s, %s County" % (
            row["d4d_score"], len(use), "" if len(use) == 1 else "s", tier_list,
            row["address"], row["city"], row["state"] or "TN", row["zip"], row["county"])
        if len(phones) > phone_cols:
            note += " | %d more in CRM" % (len(phones) - phone_cols)
        if row["known_distress"]:
            note += " | " + row["known_distress"]
        d = {"First Name": first, "Last Name": last, "Company Name": company,
             "Title": row["owner_type"], "PowerList Notes": note,
             "Property Address": row["address"], "Property City": row["city"],
             "Property State": row["state"] or "TN", "Property Zip": row["zip"],
             "Dial Tiers": tier_list, "CRM Record": row["crm_record_url"]}
        for i in range(phone_cols):
            d["Phone %d" % (i + 1)] = ("1" + use[i]["phone"]) if i < len(use) else ""
        out.append(d)
    for idx, addrs in also.items():
        out[idx]["PowerList Notes"] += " | also owns: " + "; ".join(addrs[:4]) + (
            " (+%d more)" % (len(addrs) - 4) if len(addrs) > 4 else "")
    return out, dropped, suppressed, truncated, folded


def phase_kixie(args) -> None:
    require("harvest")
    cohort = _load(COHORT_PATH, [])
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    suppress = {t.strip() for t in args.suppress_lists.split(",") if t.strip()}
    rows, dropped, suppressed, truncated, folded = kixie_rows(cohort, recs, cache, tiers, suppress,
                                                              dedupe=not args.no_dedupe)
    p = OUT / ("D4D_Top%d_Kixie_%s.csv" % (len(cohort), datetime.now().strftime("%Y%m%d")))
    write_csv(p, rows, KIXIE_FIELDS)
    n_phones = sum(1 for r in rows for i in range(KIXIE_PHONES) if r["Phone %d" % (i + 1)])
    print("kixie: %d rows (one per property, Contact Group of up to %d numbers), %d numbers (%s); "
          "%d suppressed (on %s), %d dropped with no qualifying number, %d folded into another row "
          "(same owner, every number already dialed there), %d had more than %d numbers"
          % (len(rows), KIXIE_PHONES, n_phones, ", ".join(sorted(tiers)), suppressed,
             ", ".join(sorted(suppress)) or "nothing", dropped, folded, truncated, KIXIE_PHONES))
    print("  %s" % p)
    mark_done("kixie", properties=len(rows), phones=n_phones, dropped=dropped, suppressed=suppressed,
              truncated=truncated, folded=folded, file=str(p))


# --------------------------------------------------------------------- qa --

def phase_qa(args) -> None:
    s = state()
    cohort = _load(COHORT_PATH, [])
    recs = _load(RECORDS_PATH, {})
    cache = _load(SCORES_PATH, {})
    lines = ["# D4D Top %d call list: QA summary (%s)" % (len(cohort), datetime.now().strftime("%Y-%m-%d %H:%M")), ""]

    def row(label, value, ok=None):
        mark = "" if ok is None else (" OK" if ok else " FAIL")
        lines.append("| %s | %s |%s" % (label, value, mark))

    lines += ["| Check | Value | Verdict |", "|---|---|---|"]
    row("cohort rows", len(cohort), len(cohort) == args.top)
    n_uuid = sum(1 for r in cohort if r.get("saved_uuid"))
    row("rows with a CRM record", n_uuid, n_uuid == len(cohort))
    pl = s.get("pull", {})
    row("pulled into CRM (route %s)" % pl.get("route", "?"), pl.get("pulled", 0))
    row("existing records attached to the list", pl.get("attached", 0))
    st = s.get("skiptrace", {})
    row("skip trace value spent after", "$%s" % st.get("value_spent_after", "?"),
        float(st.get("value_spent_after") or 0) == 0 if st else None)
    row("sampled new records with phones", "%s / %s" % (st.get("sample_with_phones", "?"), st.get("sample", "?")))
    hv = s.get("harvest", {})
    row("records harvested", "%s (failed %s)" % (hv.get("records", 0), hv.get("failed", 0)),
        hv.get("records", 0) == n_uuid)
    row("phones on record", hv.get("phones", 0))
    row("records with no phone", hv.get("no_phone", 0))
    elig = eligible_numbers(recs) if recs else {}
    scored = sum(1 for n in elig if (cache.get(n) or {}).get("tier"))
    row("eligible numbers scored", "%d / %d" % (scored, len(elig)), scored == len(elig) if elig else None)
    sc = s.get("score", {})
    row("trestle usage", json.dumps(sc.get("usage")) if sc.get("usage") else "n/a")
    row("estimated trestle spend", "$%.2f" % (sc.get("scored_now", 0) * pv.COST_PER_PHONE))
    wb = s.get("writeback", {})
    row("tier write-back owners / phones", "%s / %s" % (wb.get("owners", 0), wb.get("phones", 0)))
    row("write-back read-back mismatches", wb.get("mismatches", "?"), wb.get("mismatches") == 0 if wb else None)
    ex = s.get("export", {})
    row("exported properties with a callable phone", "%s / %s" % (ex.get("with_phone", 0), ex.get("properties", 0)))
    row("exported phone rows", ex.get("phones", 0))
    lines += ["", "Phone tiers in the export: %s" % json.dumps(ex.get("tiers") or {}), ""]
    lines += ["Files:"] + ["- %s" % f for f in ex.get("files") or []]
    text = "\n".join(no_dashes(x) for x in lines) + "\n"
    p = OUT / ("D4D_Top%d_Summary_%s.md" % (len(cohort), datetime.now().strftime("%Y%m%d")))
    p.write_text(text, encoding="utf-8")
    print(text)
    print("written %s" % p)
    mark_done("qa", summary=str(p))


# ------------------------------------------------------------------- main --

PHASES = {"cohort": phase_cohort, "pull": phase_pull, "skiptrace": phase_skiptrace,
          "harvest": phase_harvest, "score": phase_score, "writeback": phase_writeback,
          "export": phase_export, "dialer": phase_dialer, "kixie": phase_kixie, "qa": phase_qa}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--commit", action="store_true", help="write / spend (dry by default)")
    ap.add_argument("--master", default=DEFAULT_MASTER)
    ap.add_argument("--top", type=int, default=3000)
    ap.add_argument("--list-name", default=DEFAULT_LIST)
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--chunk", type=int, default=100, help="ids per SiftMap search")
    ap.add_argument("--chunk-pull", type=int, default=250, help="ids per add call")
    ap.add_argument("--probe-wait", type=float, default=5.0, help="minutes to wait for the probe id")
    ap.add_argument("--wait-min", type=float, default=30.0, help="minutes to wait for a batch to land")
    ap.add_argument("--no-wait", action="store_true")
    ap.add_argument("--headless", action="store_true", help="skiptrace: run the wizard headless")
    ap.add_argument("--refresh", action="store_true", help="harvest: re-read cached records")
    ap.add_argument("--refresh-pulled", action="store_true",
                    help="harvest: re-read records this run pulled (after the skip trace lands)")
    ap.add_argument("--workers", type=int, default=6, help="score: Trestle threads")
    ap.add_argument("--limit", type=int, default=0, help="score: cap numbers this run")
    ap.add_argument("--readback", type=int, default=50, help="writeback: owners to verify")
    ap.add_argument("--tiers", default="Dial First,Dial Second", help="dialer: tiers to keep")
    ap.add_argument("--phone-cols", type=int, default=10, help="dialer: Phone N columns")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="kixie: keep a number on every property that carries it (default: dial once)")
    ap.add_argument("--suppress-lists", default="Obituary",
                    help="dialer: drop any record on these lists (comma-separated)")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    PHASES[args.phase](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
