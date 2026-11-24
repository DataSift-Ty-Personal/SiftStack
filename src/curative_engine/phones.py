"""Trestle Real Contact on every living heir's numbers: does the line belong to that heir (a name match), how
reachable it is (grade, activity), the line type, and litigator risk. Uses the account's own Trestle key
(TRESTLE_PAID_API_KEY or TRESTLE_API_KEY). Dry by default: says how many pairs would be checked.

Numbers per heir: what the research found (heirs.json phones_found) plus the SmartSkip tree's numbers for a relative
with the same first and last name, at most 3 an heir. Writes houses/<slug>/phones_checked.json, the file the loader
reads; a pair already checked is never paid for twice.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

from . import common as C

REAL_CONTACT = "https://api.trestleiq.com/1.1/real_contact"
EST_USD = 0.03


def _toks(name) -> list[str]:
    return [t for t in re.sub(r"[^A-Za-z ]", " ", str(name or "")).upper().split()
            if t not in {"JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "DR"}]


def same(a, b) -> bool:
    x, y = _toks(a), _toks(b)
    return len(x) >= 2 and len(y) >= 2 and x[0] == y[0] and x[-1] == y[-1]


def legal(name) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)|\".*?\"", " ", name or "")).strip()


def pairs_for(folder) -> list[dict]:
    d = C.read_json(folder / "heirs_verified.json") or C.read_json(folder / "heirs.json")
    inp = C.read_json(folder / "input.json") or {}
    if not isinstance(d, dict):
        return []
    tree = ((inp.get("family_tree") or {}).get("relatives") or [])
    out = []
    for h in d.get("heirs") or []:
        if str(h.get("living", "")).lower() == "no":
            continue
        nums = [C.digits(p) for p in h.get("phones_found") or []]
        for r in tree:
            if same(h.get("name"), r.get("name")):
                for p in r.get("phones") or []:
                    nums.append(C.digits(p.get("number") if isinstance(p, dict) else p))
        seen = []
        for n in nums:
            if n and n not in seen:
                seen.append(n)
        for n in seen[:3]:
            out.append({"heir": legal(h.get("name")), "phone": n, "state": C.state_code(h.get("state"))})
    return out


def call(key: str, name: str, phone: str, state: str, get=None) -> dict:
    params = {"name": name, "phone": phone, "add_ons": "litigator_checks"}
    if state:
        params["address.state_code"] = state
    if get is None:
        import requests

        def get(params):
            r = requests.get(REAL_CONTACT, headers={"x-api-key": key, "Accept": "application/json"}, params=params, timeout=20)
            return r.status_code, (r.json() if r.status_code == 200 else r.text)
    for attempt in range(4):
        try:
            code, body = get(params)
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                return {"error": str(e)[:200]}
            time.sleep(2 ** attempt)
            continue
        if code == 200:
            return body if isinstance(body, dict) else {}
        if code == 429 and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        return {"error": "HTTP %s %s" % (code, str(body)[:160])}
    return {"error": "rate limited"}


def check(ws, commit: bool = False, max_pairs: int = 250, only=(), out=print, get=None) -> dict:
    """Plan (dry) or run (commit) the checks for every researched house that is still in play."""
    from .stories import collect
    cache_path = ws.stories / "vendor" / "trestle_cache.json"
    cache = C.read_json(cache_path, {}) or {}
    _, best = collect(ws)
    keep = {r["slug"] for r in best if r["verdict"] in ("Work now", "Work now, no confirmed number yet", "Research more")}
    plan = {}
    for folder in sorted(ws.houses.glob("*")) if ws.houses.exists() else []:
        slug = folder.name
        if only and slug not in only:
            continue
        if not only and slug not in keep:
            continue
        plan[slug] = pairs_for(folder)
    todo = [(s, p) for s, ps in plan.items() for p in ps if "%s|%s" % (p["heir"], p["phone"]) not in cache]
    out("houses: %d | pairs: %d | new to check: %d | about $%.2f (an estimate)"
        % (sum(1 for v in plan.values() if v), sum(len(v) for v in plan.values()), len(todo), len(todo) * EST_USD))
    if not commit:
        out("DRY: nothing checked. --commit runs the checks (cap --max-pairs %d)." % max_pairs)
        return {"pairs": len(todo)}
    if len(todo) > max_pairs:
        raise SystemExit("refused: %d pairs is over --max-pairs %d" % (len(todo), max_pairs))
    env = C.load_env()
    key = env.get("TRESTLE_PAID_API_KEY") or env.get("TRESTLE_API_KEY") or ""
    if not key and get is None:
        raise SystemExit("no Trestle key on this machine (TRESTLE_API_KEY in .env)")
    for slug, p in todo:
        res = call(key, p["heir"], p["phone"], p["state"], get=get)
        lit = ((res.get("add_ons") or {}).get("litigator_checks") or {}).get("phone.is_litigator_risk")
        cache["%s|%s" % (p["heir"], p["phone"])] = {
            "heir": p["heir"], "phone": p["phone"], "match": bool(res.get("phone.name_match")),
            "grade": res.get("phone.contact_grade") or "", "activity": res.get("phone.activity_score"),
            "linetype": res.get("phone.linetype") or "", "valid": res.get("phone.is_valid"),
            "litigator": bool(lit), "error": res.get("error"), "when": dt.datetime.now().isoformat(timespec="seconds")}
        C.write_json(cache_path, cache, indent=None)
        if get is None:
            time.sleep(0.25)
    good_total = 0
    for slug, ps in plan.items():
        if not ps:
            continue
        rows = [cache.get("%s|%s" % (p["heir"], p["phone"])) for p in ps]
        C.write_json(ws.houses / slug / "phones_checked.json", [r for r in rows if r])
        good = sum(1 for r in rows if r and r["match"] and r["grade"] in ("A", "B") and not r["litigator"])
        good_total += good
        out("  %-34s pairs %2d | the heir's own number, grade A or B, no litigator risk: %d" % (slug, len(ps), good))
    return {"pairs": len(todo), "good": good_total}
