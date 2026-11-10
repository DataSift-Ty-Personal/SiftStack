"""Top buyers for ONE deal: who actually buys near this house, at this price.

The dispo registry (`dispo_buyers.py`) answers "who buys in Knox and Blount".
This answers the narrower question a wholesaler asks the day a contract is
signed: of those, who buys THIS house, here, at this ask. Every input is a
recorded deed, never an estimate.

Phases (all free reads; nothing here spends money or writes the CRM):
  sweep    SiftMap search clipped to boxes around the subject: every arm's
           length SFR sale in the comp radius, every investor transaction in
           the buyer radius
  hydrate  deed detail per property, read through the dispo registry's cache
  rank     comps read + buyer aggregation + deal-fit score -> JSON and CSV

    python src/deal_buyer_match.py --slug 5123-odell --lat 35.914504 \\
        --lon -83.941647 --beds 3 --baths 1 --sqft 1284 --year 1952 \\
        --ask 115000 --contract 90000 --phase all

Deal-fit score, 100 points, every component a column:
  local 30     purchases inside the buyer radius, distance weighted
  price 25     share of their real purchase prices inside the ask band
  recency 20   days since their last purchase anywhere in the two counties
  product 15   share of local buys that look like this house (size, beds, age)
  volume 10    lifetime purchase count from the registry

The flip trap applies here exactly as in the registry: when SiftMap labels a
property's last sale `flip`, the current owner is a RETAIL homebuyer and the
investor is the last-sale SELLER, whose own purchase is the deed before it.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import config  # noqa: F401  loads .env
except Exception:
    pass

from siftmap_standalone import SiftMapClient  # noqa: E402
from enterprise_prospects import (  # noqa: E402
    ENTITY_RX, SearchClient, county_address, norm_name,
)
from dispo_buyers import (  # noqa: E402
    ALL_TYPES, HOLD_TYPES, K_TXN, PAGE_SIZE, _hydrate_one, local_tier,
)
from sms_agent.dispo_campaign import is_named_exclusion  # noqa: E402

log = logging.getLogger("deal_buyer_match")

ROOT = Path(__file__).resolve().parent.parent
REG_DIR = ROOT / "output" / "dispo_buyers"
NOMINAL = 10_000          # a deed under this is paper, not a purchase

# Owners that take title without being buyers a wholesaler can sell to: a
# lender's REO vehicle, a foundation or housing nonprofit land-banking a
# neighborhood. They score well on local volume, which is why they need a
# rule. Substring on the upper-cased name; every drop is logged.
NOT_BUYERS = ("GROUNDFLOOR", "FOUNDATION", "HABITAT FOR HUMANITY",
              "COMMUNITY DEVELOPMENT", "HOUSING AUTHORITY", "KCDC",
              "LAND BANK", "MINISTRIES")


def box(lat: float, lon: float, miles: float) -> list:
    dlat = miles / 69.0
    dlon = miles / (69.0 * math.cos(math.radians(lat)))
    return [{"lon": lon - dlon, "lat": lat + dlat},
            {"lon": lon + dlon, "lat": lat + dlat},
            {"lon": lon + dlon, "lat": lat - dlat},
            {"lon": lon - dlon, "lat": lat - dlat},
            {"lon": lon - dlon, "lat": lat + dlat}]


def miles_between(lat1, lon1, lat2, lon2) -> float:
    if None in (lat1, lon1, lat2, lon2):
        return 99.0
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def _since(months: int) -> str:
    return (date.today() - timedelta(days=int(months * 30.44))).isoformat()


# ---------------------------------------------------------------- sweep ----

def _paged(sc: SearchClient, filters: dict, poly: list, cap_pages: int = 20) -> list:
    out, page, total = {}, 1, None
    while page <= cap_pages:
        r = sc.search({"result_index": 1 + (page - 1) * PAGE_SIZE,
                       "with_boundaries": False, "filters": filters,
                       "addresses": [county_address("47093", "Knox", "TN")],
                       "polygon": poly})
        if total is None:
            total = r.get("total_results") or 0
        batch = r.get("data") or []
        if not batch:
            break
        for row in batch:
            rid = str(row.get("id") or "")
            if rid:
                out[rid] = {"id": rid, "address": row.get("address"),
                            "lat": row.get("latitude"), "lon": row.get("longitude"),
                            "beds": row.get("bedrooms"), "baths": row.get("bathrooms"),
                            "sqft": row.get("squareFeet"), "lot": row.get("lotAcres"),
                            "value": row.get("estimatedValue"),
                            "mls_sold": row.get("mlsSold"),
                            "corporate": row.get("corporateOwned")}
        page += 1
        if len(out) >= total:
            break
    if total and len(out) < total:
        log.warning("truncated: %d of %d rows", len(out), total)
    return list(out.values())


def phase_sweep(a, out: Path) -> None:
    sc = SearchClient(min_interval=0.4)
    base = {"type_single_family": True, "extra_last_sale_value_min": 1000,
            "extra_is_last_sale_interfamily": False}
    comps = _paged(sc, dict(base, extra_last_sale_date_min=_since(a.comp_months)),
                   box(a.lat, a.lon, a.comp_mi))
    log.info("comp sweep: %d SFR sales within %.2f mi since %s",
             len(comps), a.comp_mi, _since(a.comp_months))
    inv = {}
    for txn in ALL_TYPES:
        rows = _paged(sc, dict(base, **{K_TXN: [txn],
                                        "extra_last_sale_date_min": _since(a.buyer_months)}),
                      box(a.lat, a.lon, a.buyer_mi))
        for r in rows:
            r["txn"] = txn
        inv[txn] = rows
        log.info("buyer sweep %-9s %d properties within %.1f mi", txn, len(rows), a.buyer_mi)
    if not comps and not any(inv.values()):
        raise SystemExit("GATE FAILED: both sweeps returned nothing; check the polygon")
    _save(out / "sweep.json", {"comps": comps, "investor": inv,
                               "args": vars(a), "when": date.today().isoformat()})


# -------------------------------------------------------------- hydrate ----

def phase_hydrate(a, out: Path) -> None:
    sw = _load(out / "sweep.json", None)
    if not sw:
        raise SystemExit("run --phase sweep first")
    ids = {r["id"] for r in sw["comps"]}
    ids |= {r["id"] for rows in sw["investor"].values() for r in rows}
    if a.subject_id:
        ids.add(str(a.subject_id))
    local = _load(out / "details.json", {})
    shared = _load(REG_DIR / "details.json", {})
    # The registry cache is two months old for deeds; a property that sold
    # again since then must be re-read, so reuse it only when its newest deed
    # is already inside this sweep's window.
    for i in ids:
        if i not in local and i in shared and not shared[i].get("miss"):
            sh = shared[i].get("sale_history") or []
            if sh and str(sh[0].get("date") or "") >= _since(a.comp_months):
                local[i] = shared[i]
    todo = [i for i in ids if i not in local]
    log.info("%d properties, %d cached, %d to fetch", len(ids), len(ids) - len(todo), len(todo))
    smc = SiftMapClient()
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_hydrate_one, smc, i): i for i in todo}
        for f in as_completed(futs):
            i = futs[f]
            try:
                local[i] = f.result()
            except Exception as e:  # noqa: BLE001
                local[i] = {"id": i, "miss": str(e)[:200]}
            done += 1
            if done % 50 == 0:
                _save(out / "details.json", local)
                log.info("hydrated %d/%d", done, len(todo))
    _save(out / "details.json", local)
    miss = sum(1 for i in ids if local.get(i, {}).get("miss"))
    if ids and miss == len(ids):
        raise SystemExit("GATE FAILED: every detail call failed; this is AUTH, not an empty market")
    log.info("hydrate done: %d misses of %d", miss, len(ids))


# ----------------------------------------------------------------- rank ----

def _real(price) -> bool:
    return bool(price) and price >= NOMINAL


def _comps(a, sw: dict, det: dict) -> dict:
    lo, hi = a.sqft * 0.70, a.sqft * 1.35
    rows = []
    for r in sw["comps"]:
        d = det.get(r["id"]) or {}
        sh = d.get("sale_history") or []
        if not sh or not _real(sh[0].get("price")):
            continue
        s = sh[0]
        if str(s.get("date") or "") < _since(a.comp_months):
            continue
        buyer = (s.get("buyer") or "").strip()
        rows.append({
            "address": r["address"], "dist": round(miles_between(a.lat, a.lon, r["lat"], r["lon"]), 2),
            "date": s.get("date"), "price": s.get("price"), "beds": r.get("beds"),
            "baths": r.get("baths"), "sqft": r.get("sqft"),
            "ppsf": round(s["price"] / r["sqft"]) if r.get("sqft") else None,
            "buyer": buyer, "seller": (s.get("seller") or "").strip(),
            "cash": bool(s.get("cash")), "entity_buyer": bool(ENTITY_RX.search(buyer.upper())),
            "mls_sold": r.get("mls_sold"),
            "in_size": bool(r.get("sqft") and lo <= r["sqft"] <= hi),
        })
    rows.sort(key=lambda x: x["dist"])
    sized = [x for x in rows if x["in_size"]]
    retail = [x for x in sized if not x["cash"] and not x["entity_buyer"]]
    asis = [x for x in sized if x["cash"] or x["entity_buyer"]]
    def stats(xs):
        p = [x["price"] for x in xs]
        f = [x["ppsf"] for x in xs if x["ppsf"]]
        return {"n": len(xs), "median": median(p) if p else None,
                "median_ppsf": median(f) if f else None,
                "lo": min(p) if p else None, "hi": max(p) if p else None}
    return {"rows": rows, "retail": stats(retail), "asis": stats(asis),
            "size_window": [round(lo), round(hi)]}


def _registry_index() -> tuple:
    reg = _load(REG_DIR / "registry.json", [])
    prof = {p["buyer_key"]: p for p in _load(REG_DIR / "buyer_profiles.json", [])}
    idx = {}
    for b in reg:
        for n in [b.get("name")] + list(b.get("aliases") or []):
            if n:
                idx.setdefault(norm_name(n), b)
    return idx, prof


def _acquisition_lookup() -> dict:
    """address -> sale_history, from the registry's own sweep and deed cache.

    The registry records an EXITED flip at its exit price (the deed to the
    retail buyer), so a flipper's price band is built on resale prices: GDP
    Properties read a $307,500 median on a book of $100K-$200K buys. The
    acquisition is the deed before the exit where the flipper is the buyer.
    """
    rows = _load(REG_DIR / "rows.json", {})
    det = _load(REG_DIR / "details.json", {})
    out = {}
    for seg in rows.values():
        for r in seg:
            d = det.get(str(r.get("id"))) or {}
            if r.get("address") and d.get("sale_history"):
                out[r["address"]] = d["sale_history"]
    return out


def _buy_price(b: dict, name_key: str, acq: dict):
    """What this buyer PAID on a registry buy row, or None when unknowable."""
    if b.get("role") != "exited":
        return b.get("price")
    for s in (acq.get(b.get("address") or "") or [])[1:]:
        if norm_name(s.get("buyer") or "") == name_key:
            return s.get("price")
    return None


def _observations(a, sw: dict, det: dict) -> list:
    """One row per (buyer, property): who paid what, where, and when."""
    obs, seen = [], set()
    for txn, rows in sw["investor"].items():
        for r in rows:
            d = det.get(r["id"]) or {}
            sh = d.get("sale_history") or []
            if d.get("miss") or not sh:
                continue
            if txn in HOLD_TYPES:
                who = d.get("owner_name") or sh[0].get("buyer") or ""
                buy, exit_price, role = sh[0], None, "holder"
            else:
                who = (sh[0].get("seller") or "").strip()
                nk = norm_name(who)
                buy = next((s for s in sh[1:] if norm_name(s.get("buyer") or "") == nk),
                           sh[1] if len(sh) > 1 else {})
                exit_price, role = sh[0].get("price"), "flipped"
            if not who:
                continue
            key = (norm_name(who), r["id"])
            if key in seen:
                continue
            seen.add(key)
            obs.append({"buyer": who, "id": r["id"], "address": r["address"],
                        "dist": miles_between(a.lat, a.lon, r["lat"], r["lon"]),
                        "date": str(buy.get("date") or sh[0].get("date") or ""),
                        "exit_date": str(sh[0].get("date") or "")[:10] if exit_price else "",
                        "price": buy.get("price"), "exit_price": exit_price,
                        "cash": bool(buy.get("cash")), "txn": txn, "role": role,
                        "beds": r.get("beds"), "sqft": r.get("sqft")})
    # Entity buyers inside the comp radius that SiftMap did not label as an
    # investor transaction. The label lags (rental reads ZERO at 12 months),
    # so an LLC that bought down the street last month is often unlabeled.
    for r in sw["comps"]:
        d = det.get(r["id"]) or {}
        sh = d.get("sale_history") or []
        if not sh:
            continue
        dist = miles_between(a.lat, a.lon, r["lat"], r["lon"])
        who = (sh[0].get("buyer") or "").strip()
        if who and ENTITY_RX.search(who.upper()) and (norm_name(who), r["id"]) not in seen:
            seen.add((norm_name(who), r["id"]))
            obs.append({"buyer": who, "id": r["id"], "address": r["address"], "dist": dist,
                        "date": str(sh[0].get("date") or ""), "price": sh[0].get("price"),
                        "exit_price": None, "cash": bool(sh[0].get("cash")),
                        "txn": "unlabeled", "role": "entity buy",
                        "beds": r.get("beds"), "sqft": r.get("sqft")})
        # An unlabeled flip EXIT: the seller bought it within two years. An
        # entity seller qualifies on that alone; a person also needs a resale
        # at 1.35x or more, or every ordinary short-hold homeowner would count.
        # Unique Construct LLC finished 2416 Alberta Dr three streets from
        # 5123 Odell ($155,000 to $362,000) and SiftMap never called it a flip.
        seller = (sh[0].get("seller") or "").strip()
        if (seller and len(sh) > 1 and sh[0].get("date")
                and norm_name(sh[1].get("buyer") or "") == norm_name(seller)
                and (norm_name(seller), r["id"]) not in seen):
            held = str(sh[1].get("date") or "")
            bp, xp = sh[1].get("price") or 0, sh[0].get("price") or 0
            recent = held and held >= str(date.fromisoformat(str(sh[0]["date"])[:10])
                                          - timedelta(days=730))
            entity = bool(ENTITY_RX.search(seller.upper()))
            if recent and (entity or (_real(bp) and xp >= 1.35 * bp)):
                seen.add((norm_name(seller), r["id"]))
                obs.append({"buyer": seller, "id": r["id"], "address": r["address"],
                            "dist": dist, "date": held, "price": bp,
                            "exit_price": xp, "exit_date": str(sh[0]["date"])[:10],
                            "cash": bool(sh[1].get("cash")),
                            "txn": "flip (deed)", "role": "flipped",
                            "beds": r.get("beds"), "sqft": r.get("sqft")})
    return obs


def _condo(address: str) -> bool:
    up = " " + (address or "").upper() + " "
    return "#" in up or " APT " in up or " UNIT " in up


def _w(dist: float) -> float:
    return 1.0 if dist <= 1 else 0.75 if dist <= 2 else 0.5 if dist <= 3 else 0.3


def phase_rank(a, out: Path) -> None:
    sw = _load(out / "sweep.json", None)
    det = _load(out / "details.json", {})
    if not sw or not det:
        raise SystemExit("run --phase sweep and --phase hydrate first")
    comps = _comps(a, sw, det)
    obs = _observations(a, sw, det)
    idx, prof = _registry_index()
    acq = _acquisition_lookup()
    band_lo, band_hi = a.ask * a.band_lo, a.ask * a.band_hi
    today = date.today()

    groups = defaultdict(list)
    for o in obs:
        groups[norm_name(o["buyer"])].append(o)

    ranked, dropped = [], []
    for k, os_ in groups.items():
        name = max((o["buyer"] for o in os_), key=len)
        reg = idx.get(k) or {}
        p = prof.get(reg.get("buyer_key", ""), {})
        n_life = max(reg.get("n_buys") or 0, len(os_))
        excl = is_named_exclusion(name) or next(
            (n for n in NOT_BUYERS if n in name.upper()), "")
        tier, why, _ = local_tier(name.upper(), n_life)
        if excl or tier == "EXCLUDE":
            dropped.append({"buyer": name, "why": excl or why, "local_buys": len(os_)})
            continue
        # A person who bought once, unlabeled, is a homebuyer paying cash.
        if not ENTITY_RX.search(name.upper()) and n_life < 2:
            dropped.append({"buyer": name, "why": "one purchase, person", "local_buys": len(os_)})
            continue

        local_w = sum(_w(o["dist"]) for o in os_)
        prices = [o["price"] for o in os_ if _real(o["price"])]
        local_addr = {o["address"] for o in os_}
        reg_key = norm_name(reg.get("name") or name)
        for b in reg.get("buys") or []:
            if b.get("address") in local_addr or str(b.get("date") or "") < _since(36):
                continue
            bp = _buy_price(b, reg_key, acq)
            if _real(bp):
                prices.append(bp)
        in_band = [x for x in prices if band_lo <= x <= band_hi]
        price_fit = len(in_band) / len(prices) if prices else 0.0
        # A flip that just SOLD is activity too: that operator has cash back.
        dates = [o["date"] for o in os_ if o["date"]]
        dates += [o.get("exit_date") or "" for o in os_]
        dates += [str(reg.get("last_buy") or ""), str(p.get("last_buy") or "")]
        dates += [str(b.get("date") or "") for b in reg.get("buys") or []]
        last = max(d for d in dates if d) if any(dates) else ""
        days = (today - date.fromisoformat(last[:10])).days if last else 9999
        similar = [o for o in os_
                   if o.get("sqft") and not _condo(o["address"])
                   and a.sqft * 0.65 <= o["sqft"] <= a.sqft * 1.5
                   and (o.get("beds") or 0) in (a.beds - 1, a.beds, a.beds + 1)]
        exited = sum(1 for o in os_ if o["role"] == "flipped") + (reg.get("n_exited") or 0)

        pts = {
            "local": min(30.0, 10.0 * local_w),
            "price": 25.0 * price_fit * min(1.0, len(prices) / 3.0),
            "recency": 20 if days <= 90 else 16 if days <= 180 else 10 if days <= 365
                       else 4 if days <= 730 else 0,
            "product": 15.0 * (len(similar) / len(os_)),
            "volume": 10 if n_life >= 10 else 7 if n_life >= 5 else 5 if n_life >= 3
                      else 3 if n_life == 2 else 1,
        }
        score = round(sum(pts.values()), 1)
        ranked.append({
            "buyer": name, "score": score, "pts": {k2: round(v, 1) for k2, v in pts.items()},
            "local_buys": len(os_), "local_within_1mi": sum(1 for o in os_ if o["dist"] <= 1),
            "nearest_mi": round(min(o["dist"] for o in os_), 2),
            "lifetime_buys": n_life, "exited": exited,
            "days_since_last": days, "last_buy": last[:10],
            "price_med": median(prices) if prices else None,
            "price_lo": min(prices) if prices else None, "price_hi": max(prices) if prices else None,
            "in_band": len(in_band), "priced": len(prices),
            "cash_pct": round(100 * sum(1 for o in os_ if o["cash"]) / len(os_)),
            "tier": tier, "tier_reason": why,
            "buyer_type": p.get("buyer_type") or "", "mail": reg.get("mail") or "",
            "principal": p.get("principal") or reg.get("principal") or "",
            "principal_source": p.get("principal_source") or "",
            "registry_key": reg.get("buyer_key") or "",
            "saved_uuid": reg.get("saved_uuid") or p.get("saved_uuid") or "",
            "best_tier": p.get("best_tier") or "", "reachable": p.get("reachable"),
            "local": sorted(({"address": o["address"], "dist": round(o["dist"], 2),
                              "date": o["date"], "price": o["price"], "exit": o["exit_price"],
                              "txn": o["txn"], "beds": o["beds"], "sqft": o["sqft"],
                              "cash": o["cash"]} for o in os_), key=lambda x: x["date"], reverse=True),
        })
    ranked.sort(key=lambda r: (-r["score"], -r["local_buys"]))
    # The call list is repeat closers only. A two-purchase LLC can outscore a
    # 23-flip operator on recency alone, and a blast is not the place to find
    # out whether someone closes. They stay ranked, just not on the top N.
    # One slot per OPERATOR: groups.json (research layer, every grouping cited)
    # ties LLCs one family runs, which a name join can never see. South Knox 12
    # and 2614 Dexter both file from one Mt Juliet house; unchecked, a family
    # took two of ten slots.
    groups = {}
    for g in (_load(out / "groups.json", {}) or {}).get("groups", []):
        for m in g.get("members") or []:
            groups[norm_name(m)] = g["name"]
    n, slot_of = 0, {}
    for r in ranked:
        r["top"] = 0
        keys = {norm_name(r["buyer"]), norm_name(r.get("registry_key") or "")}
        r["group"] = next((groups[k] for k in keys if k in groups), "")
        if r["group"] and r["group"] in slot_of:
            r["same_group_as"] = slot_of[r["group"]]
            continue
        if r["lifetime_buys"] >= a.min_buys and n < a.top:
            n += 1
            r["top"] = n
            if r["group"]:
                slot_of[r["group"]] = n
    result = {"subject": {k: getattr(a, k) for k in
                          ("address", "lat", "lon", "beds", "baths", "sqft", "year",
                           "ask", "contract")},
              "band": [round(band_lo), round(band_hi)], "comps": comps,
              "observations": len(obs), "ranked": ranked, "dropped": dropped}
    _save(out / "ranked.json", result)
    with (out / "ranked.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["top", "buyer", "score", "local_buys", "local_within_1mi", "nearest_mi",
                "lifetime_buys", "exited", "days_since_last", "price_med", "price_lo",
                "price_hi", "in_band", "priced", "cash_pct", "buyer_type", "principal",
                "best_tier", "mail"]
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(ranked)

    c = comps
    print("comps (%s-%s sqft, %d mo, %.2f mi): retail n=%s median %s ($%s/sf) | as-is n=%s median %s ($%s/sf)"
          % (c["size_window"][0], c["size_window"][1], a.comp_months, a.comp_mi,
             c["retail"]["n"], c["retail"]["median"], c["retail"]["median_ppsf"],
             c["asis"]["n"], c["asis"]["median"], c["asis"]["median_ppsf"]))
    print("observations %d, buyers ranked %d, dropped %d" % (len(obs), len(ranked), len(dropped)))
    for i, r in enumerate(ranked[: a.show], 1):
        print("%2d %s %5.1f %-42s local %d (1mi %d, near %.2f) life %d last %sd med %s band %d/%d %s"
              % (i, ("T%-2d" % r["top"]) if r["top"] else "   ", r["score"], r["buyer"][:42], r["local_buys"], r["local_within_1mi"],
                 r["nearest_mi"], r["lifetime_buys"], r["days_since_last"], r["price_med"],
                 r["in_band"], r["priced"], r["buyer_type"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", choices=["sweep", "hydrate", "rank", "all"], default="all")
    ap.add_argument("--slug", required=True)
    ap.add_argument("--address", default="")
    ap.add_argument("--subject-id", default="")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--beds", type=int, required=True)
    ap.add_argument("--baths", type=float, default=1)
    ap.add_argument("--sqft", type=float, required=True)
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--ask", type=float, required=True, help="price we offer buyers")
    ap.add_argument("--contract", type=float, default=0)
    ap.add_argument("--comp-mi", type=float, default=1.0)
    ap.add_argument("--comp-months", type=int, default=18)
    ap.add_argument("--buyer-mi", type=float, default=3.0)
    ap.add_argument("--buyer-months", type=int, default=36)
    ap.add_argument("--band-lo", type=float, default=0.6,
                    help="ask band floor multiple; below the band is interest, not affordability")
    ap.add_argument("--band-hi", type=float, default=1.6)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-buys", type=int, default=3,
                    help="lifetime purchases required for the top N call list")
    ap.add_argument("--show", type=int, default=30)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    out = ROOT / "output" / "deal_buyers" / a.slug
    if a.phase in ("sweep", "all"):
        phase_sweep(a, out)
    if a.phase in ("hydrate", "all"):
        phase_hydrate(a, out)
    if a.phase in ("rank", "all"):
        phase_rank(a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
