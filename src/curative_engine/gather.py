"""The free public reads: each house on its county's tax roll and the city's own bill (where the county has an
adapter), and each owner of a likely death against the county's obituary index. Resumable: everything read is
kept in the work folder and a re-run reads only what is missing or failed.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import common as C
from .counties import county_for


def _county_of(x: dict, details: dict):
    rec = (x or {}).get("record") or {}
    sm = details.get(str(rec.get("dataflik_id"))) or {}
    addr = rec.get("address") or {}
    return county_for(fips=sm.get("fips") or "", name=addr.get("county") or sm.get("county") or "",
                      state=addr.get("state") or "")


def county(ws, workers: int = 4, out=print, read_roll=None, read_city=None) -> dict:
    """The county roll for every record in scope whose county has a roll, then the city bill for every parcel
    the roll codes inside a city with an adapter."""
    from .counties import read_roll as _read_roll
    read_roll = read_roll or _read_roll
    records = ws.read("records.json", {}) or {}
    details = ws.read("details.json", {}) or {}
    scope = set((ws.read("scope.json", {}) or {}).get("uuids") or records.keys())
    done = ws.read("county.json", {}) or {}
    todo, skipped = [], Counter()
    for u in scope:
        x = records.get(u) or {}
        rec = x.get("record") or {}
        if not rec:
            continue
        cty = _county_of(x, details)
        if not cty.roll_key:
            skipped[cty.label] += 1
            continue
        if u in done and done[u].get("status") not in ("error", None):
            continue
        o = rec.get("owner") or {}
        owner = " ".join(str(v or "") for v in (o.get("first_name"), o.get("last_name"), o.get("company"))).strip()
        todo.append((u, cty.roll_key, (rec.get("address") or {}).get("street") or "", owner))
    for label, n in skipped.most_common():
        out("  %s: no county tax roll is read here (%d house(s) ranked without it)" % (label, n))
    out("county roll: %d to read, %d already read" % (len(todo), len(done)))
    lock = threading.Lock()
    n = [0]

    def one(item):
        u, roll, street, owner = item
        try:
            return u, read_roll(roll, street, owner)
        except Exception as exc:  # noqa: BLE001  (an unanswered county is "error": the next run asks again)
            return u, {"status": "error", "error": str(exc)[:160]}

    with ThreadPoolExecutor(max(1, int(workers))) as ex:
        for f in as_completed([ex.submit(one, it) for it in todo]):
            u, res = f.result()
            with lock:
                done[u] = res
                n[0] += 1
                if n[0] % 100 == 0:
                    ws.write("county.json", done, indent=None)
                    out("  %d/%d" % (n[0], len(todo)))
    ws.write("county.json", done, indent=None)
    # ---- the city's own bill
    city = ws.read("city.json", {}) or {}
    asked = 0
    for u, res in list(done.items()):
        x = records.get(u) or {}
        if not x:
            continue
        cty = _county_of(x, details)
        for a in res.get("accounts") or []:
            codes = {str(b.get("city_no") or "") for b in a.get("bills") or []}
            hit = sorted(codes & set(cty.city_codes))
            pid = str(a.get("account") or "").replace("-", "").replace(" ", "")
            if not hit or not pid or pid in city:
                continue
            reader = read_city or cty.city_codes[hit[0]][1]
            got = reader(pid)
            asked += 1
            if got is not None:
                got["key"] = u
                city[pid] = got
            time.sleep(0.9 if read_city is None else 0)
            if asked % 50 == 0:
                ws.write("city.json", city, indent=None)
    ws.write("city.json", city, indent=None)
    st = Counter(v.get("status") for v in done.values())
    out("county roll read: %s | city bills read this run: %d" % (dict(st), asked))
    return {"roll": dict(st), "city_asked": asked}


def obituary_keys(rows: list[dict]) -> list[tuple[str, str, str]]:
    """(fips, LAST, FIRST) for the first two owners of every house worth checking: a likely death, not gated,
    in a value band worth working, and not already settled by a court notice or research."""
    want = []
    for r in rows:
        if r.get("gate") or (r.get("death_conf") or 0) < 1:
            continue
        if not (100000 <= (r.get("avm") or 0) <= 900000):
            continue
        if r.get("decedent_court") or r.get("research_status"):
            continue
        for n in list(r.get("deed") or [])[:2]:
            t = C.name_toks(n)
            if len(t) >= 2:
                want.append((r.get("fips") or "", t[-1], t[0]))
    return list(dict.fromkeys(want))


def obituaries(ws, rows: list[dict] | None = None, workers: int = 2, pause: float = 1.2, out=print) -> dict:
    """Each owner name against its county's obituary index (where the county has one). Kept in obits.json as
    "fips|LAST|FIRST" -> the index rows (text); a failed lookup is not kept, so the next run asks again."""
    rows = rows if rows is not None else (ws.read("joined.json", []) or [])
    cache = ws.read("obits.json", {}) or {}
    todo = []
    for fips, last, first in obituary_keys(rows):
        cty = county_for(fips=fips)
        if not cty.obituaries:
            continue
        k = "%s|%s|%s" % (fips, last, first)
        if k not in cache:
            todo.append((k, cty, last, first))
    out("obituary index: %d name(s) to look up, %d already looked up" % (len(todo), len(cache)))
    lock = threading.Lock()
    done = [0]

    def one(item):
        k, cty, last, first = item
        txt = cty.obituaries(last, first)
        if pause:
            time.sleep(pause)
        with lock:
            if txt is not None:
                cache[k] = txt
            done[0] += 1
            if done[0] % 50 == 0:
                ws.write("obits.json", cache, indent=None)

    with ThreadPoolExecutor(max(1, int(workers))) as ex:
        list(ex.map(one, todo))
    ws.write("obits.json", cache, indent=None)
    return {"asked": len(todo), "kept": len(cache)}
