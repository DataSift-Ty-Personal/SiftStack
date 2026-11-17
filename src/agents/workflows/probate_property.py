"""probate_property: the decedent's house for a probate filing that names no address.

A probate notice names the person who died and the personal representative, never the house:
its only street addresses belong to the court, the attorney or the representative. So the house
is found on the county's own roll, by the decedent's name. Both rolls match names loosely, which
is why every result passes property_lookup's owner-of-record guard (_owner_matches_decedent): it
counts only when the owner of record carries the decedent's surname AND first name (a maiden name
and AKA names count). Before the guard, 3 of 4 houses "found" in one Blount run belonged to living
strangers who shared part of a name. A miss costs one lead; a wrong house sends a probate letter
and a caller to an unrelated family.

  Knox    the county tax API (knox-tn.mygovonline.com, plain JSON) by the decedent's name, then,
          when the ask names an executor, by the executor's name for a parcel the decedent
          co-owns. Both through the guard, both needing a street address (the county returns
          business accounts with none, and one of those crashed a 103-notice run). Called here
          directly: tax_enricher imports notice_parser, which imports Playwright at module scope,
          and property_lookup's own Knox path drives KGIS in a browser. The brain has no browser.
  Blount  TPAD's own JSON endpoint (property_lookup._tpad_lookup), house number moved to the front.

Not used, on purpose: the people-search tier (a name plus a city returns whoever ranks first, and
the trace rule is never to look a person up that way) and Smarty (lapsed). The county gives a
street line only, so one SiftMap address search fills the city and ZIP, kept only for the same
house number and street inside the same county.

Free and read-only: nothing is written to DataSift. The answer is the house, or an honest "no
property found" with how many county results were turned away and why; a file in the run folder
lists every candidate considered.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .. import slack
from ..autopilot.sources import same_street
from . import skip_trace as common

SRC = Path(__file__).resolve().parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

WORKFLOW = "probate_property"
KNOX_API_BASE = "https://knox-tn.mygovonline.com/api/v2"       # the same base tax_enricher reads
DEFAULT_CITY = {"knox": "Knoxville", "blount": "Maryville"}
COUNTIES = ("knox", "blount")
EXECUTOR_RX = re.compile(r"\b(?:executor|executrix|administrator|personal representative|pr)\s*[:=-]\s*([A-Za-z][A-Za-z .,'-]{2,60})", re.I)


def lookup_module():
    """property_lookup, or (None, why). It imports beautifulsoup4 at module scope for its browser
    path, and a box without that package must say so rather than fail in the middle of an ask."""
    try:
        import property_lookup as pl
        return pl, ""
    except ImportError as exc:
        return None, ("the county lookup could not load on this box (%s); the brain image needs beautifulsoup4" % exc)


def knox_search(name: str, *, get=None) -> list[dict]:
    """The Knox tax API's name search: [{owner, parcel_address, account_number, ...}]. A 404 or any
    failure is an empty answer, which the caller reports as a miss, never as a house."""
    import requests
    get = get or requests.get
    try:
        r = get("%s/parcels/%s?detail_level=public&start=0&length=10" % (KNOX_API_BASE, quote(name)), timeout=30)
        if getattr(r, "status_code", 200) == 404:
            return []
        r.raise_for_status()
        return list((r.json() or {}).get("parcels") or [])
    except Exception:  # noqa: BLE001
        return []


def _overlap(search: str, owner: str) -> float:
    a = set(search.upper().split()) - {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    b = set(owner.upper().split()) - {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    return len(a & b) / max(len(a), len(b)) if a and b else 0.0


def _knox_rows(pl, decedent: str, queries: list, knox, sleep, tier: str, log: list) -> list[dict]:
    """Candidates from the Knox roll for each query in turn, the first query whose results survive
    the guard winning. Every result is logged with the reason it was kept or turned away."""
    for i, (label, q) in enumerate(queries):
        if i:
            sleep()
        kept = []
        for p in knox(q):
            owner, street = str(p.get("owner") or "").strip(), str(p.get("parcel_address") or "").strip()
            why = ("no street address (a business or personal property account)" if not street else
                   "owner of record is not the decedent" if not pl._owner_matches_decedent(decedent, owner) else "")
            log.append({"county": "Knox", "tier": tier, "query": q, "owner": owner, "street": street,
                        "parcel": str(p.get("account_number") or ""), "kept": not why, "why": why or "kept"})
            if not why:
                kept.append({"owner": owner, "address": street, "parcel_id": str(p.get("account_number") or ""),
                             "classification": str(p.get("property_type") or p.get("class") or "Residential"),
                             "score": _overlap(q, owner), "query": q, "tier": tier})
        if kept:
            return sorted(kept, key=lambda c: -c["score"])
    return []


def _blount_rows(pl, decedent: str, queries: list, tpad, sleep, log: list) -> list[dict]:
    for i, (label, q) in enumerate(queries):
        if i:
            sleep()
        kept = []
        for r in tpad(q):
            owner, street = str(r.get("owner") or "").strip(), str(r.get("address") or "").strip()
            why = ("no street address" if not street else
                   "owner of record is not the decedent" if not pl._owner_matches_decedent(decedent, owner) else "")
            log.append({"county": "Blount", "tier": "decedent", "query": q, "owner": owner, "street": street,
                        "parcel": str(r.get("parcel_id") or ""), "kept": not why, "why": why or "kept"})
            if not why:
                kept.append(dict(r, query=q, tier="decedent"))
        if kept:
            return kept
    return []


def fill_city_zip(street: str, county: str, map_client) -> dict:
    """City and ZIP for a street line from the county roll: one SiftMap address search, a row kept
    only at the same house number and street inside the same county and state."""
    for text in ("%s, %s, TN" % (street, DEFAULT_CITY.get(county.lower(), "")), "%s, TN" % street):
        rows = (map_client.search({"addresses": [{"type": "address", "search": text}], "size": 10}) or {}).get("data") or []
        for r in rows:
            addr = str(r.get("address") or "")
            if not same_street(street, re.split(r"[,;]", addr)[0]):
                continue
            if str(r.get("county") or "").strip().lower() != county.lower() or str(r.get("state") or "TN").upper() != "TN":
                continue
            parts = [p.strip() for p in addr.split(",")]
            m = re.search(r"\b(\d{5})\b", parts[-1] if parts else "")
            return {"city": parts[1] if len(parts) >= 3 else "", "zip": m.group(1) if m else "", "property_id": str(r.get("id") or "")}
    return {}


def find(decedent: str, county: str, executor: str = "", *, pl=None, knox=None, tpad=None, map_client=None,
         sleep=None) -> dict:
    """One county's answer for one decedent. Reads public county data only; writes nothing."""
    log: list = []
    county_l = county.strip().lower()
    out = {"county": county.title(), "found": None, "alternates": [], "candidates": log, "rejected": 0}
    if county_l not in COUNTIES:
        out["miss"] = "only Knox and Blount are wired (%s is not)" % county
        return out
    delay = common.setting("AGENTS_PROBATE_DELAY_S", 1.5)
    sleep = sleep or (lambda: time.sleep(delay))
    queries = pl._search_variants(decedent)
    if not queries:
        out["miss"] = "the decedent's name has no first and last name to search"
        return out
    if county_l == "knox":
        kept = _knox_rows(pl, decedent, queries, knox or knox_search, sleep, "decedent", log)
        if not kept and executor:
            # A parcel the decedent holds WITH the executor: the guard still demands the decedent on the
            # title, because the executor's own house is not the estate's (every executor-tier match in
            # the September 2026 Knox run was the executor's own home).
            kept = _knox_rows(pl, decedent, pl._search_variants(executor)[:3], knox or knox_search, sleep, "executor", log)
    else:
        kept = _blount_rows(pl, decedent, queries, tpad or pl._tpad_lookup, sleep, log)
    out["rejected"] = sum(1 for c in log if not c["kept"])
    if not kept:
        out["miss"] = "no property found: %d county result(s) were checked and none is owned by the decedent" % out["rejected"] \
            if out["rejected"] else "no property found on the %s roll under any form of the name" % county.title()
        return out
    best = pl._select_best_property(kept, "")
    street = best.get("address") or ""
    if county_l == "blount":
        street = pl._normalize_tpad_address(street)
    found = {"street": street, "city": "", "state": "TN", "zip": "", "parcel_id": best.get("parcel_id") or "",
             "owner_of_record": best.get("owner") or "", "county": county.title(), "tier": best.get("tier") or "decedent",
             "query": best.get("query") or ""}
    if map_client is not None and street:
        try:
            found.update({k: v for k, v in fill_city_zip(street, county_l, map_client).items() if v})
        except Exception as exc:  # noqa: BLE001  (the house is still the answer; the city and ZIP are a convenience)
            out["city_zip_problem"] = "the city and ZIP lookup failed: %s" % str(exc)[:100]
    found["city"] = found["city"] or DEFAULT_CITY[county_l]
    found["city_guessed"] = not found.get("zip")
    out["found"] = found
    out["alternates"] = [{"street": (pl._normalize_tpad_address(c["address"]) if county_l == "blount" else c["address"]),
                          "owner_of_record": c.get("owner") or "", "parcel_id": c.get("parcel_id") or ""}
                         for c in kept if c is not best][:4]
    return out


def run(task: dict, *, pl=None, knox=None, tpad=None, map_client=None, sleep=None) -> dict:
    inp = common.inputs_of(task)
    decedent = " ".join(str(inp.get("decedent") or "").split())
    county = str(inp.get("county") or "").strip().lower()
    details = common.details_of(task)
    if not decedent:
        return {"ok": False, "say": "Name the person who died, as the notice prints it."}
    counties = list(COUNTIES) if county in ("", "both", "either", "unknown", "knox or blount") else \
        [c for c in COUNTIES if c in county]
    if not counties:
        return {"ok": False, "say": "Only Knox and Blount are wired; say Knox, Blount or both."}
    m = EXECUTOR_RX.search(details or "")
    executor = " ".join(m.group(1).split()).strip(" ,.") if m else ""
    if pl is None:
        pl, why = lookup_module()
        if pl is None:
            return {"ok": True, "decedent": decedent, "problems": [why], "results": [], "head": "The county lookup did not run."}
    if map_client is None:
        try:
            import buyer_sweep
            map_client = buyer_sweep.MapClient()
        except Exception:  # noqa: BLE001
            map_client = None
    results = [find(decedent, c, executor, pl=pl, knox=knox, tpad=tpad, map_client=map_client, sleep=sleep) for c in counties]
    hits = [r for r in results if r.get("found")]
    files = common.files_dir(task, WORKFLOW)
    name = "probate_candidates_%s.json" % (task.get("id") or 0)
    (files / name).write_text(json.dumps({"decedent": decedent, "executor": executor, "results": results}, indent=1,
                                         default=str), encoding="utf-8")
    problems = [r["city_zip_problem"] for r in results if r.get("city_zip_problem")]
    if len(hits) > 1:
        head = "Found a house owned by the decedent in %s; a person picks the right one." % " and ".join(r["county"] for r in hits)
    elif hits:
        f = hits[0]["found"]
        head = "%s, %s TN%s (parcel %s), owner of record %s." % (
            f["street"], f["city"], (" " + f["zip"]) if f.get("zip") else "", f["parcel_id"] or "unknown", f["owner_of_record"])
    else:
        head = "No property found for %s." % decedent
    return {"ok": True, "decedent": decedent, "executor": executor, "results": results, "head": head,
            "problems": problems, "file": name, "run_dir": str(files.parent)}


def render(task: dict, result: dict) -> str:
    if not result.get("ok"):
        return result.get("say") or ""
    icon = ":warning:" if result.get("problems") else ":white_check_mark:"
    out = ["%s *probate property* %s: %s" % (icon, result.get("decedent") or "", result.get("head") or "")]
    for r in result.get("results") or []:
        f = r.get("found")
        if f:
            how = "the executor's search, the decedent on the title" if f.get("tier") == "executor" else "the decedent's name"
            out.append("  %s: %s (found by %s%s)" % (r["county"], f["street"], how,
                                                     "; city is the county seat, no ZIP found" if f.get("city_guessed") else ""))
            for a in r.get("alternates") or []:
                out.append("    also on the roll: %s" % a["street"])
        else:
            out.append("  %s: %s" % (r["county"], r.get("miss") or "nothing found"))
    if result.get("file"):
        url = slack.file_url(int(task.get("id") or 0), result["file"])
        if url:
            out.append("  Every candidate and why: <%s|%s>" % (url, result["file"]))
    for p in (result.get("problems") or [])[:3]:
        out.append(":small_orange_diamond: %s" % p)
    return "\n".join(out)
