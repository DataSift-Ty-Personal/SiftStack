"""Visual Distress scoring: driving for dollars, without the driving.

Nic's Lambda scores a property's exterior condition from its Google Street View
image using a trained CLIP model, returning a 0-100 `visual_distress_score`, nine
component scores and an LLM narrative. This module is the client: it decides what
to score, protects the spend, and reads the results back honestly.

    doctor      one image, proves the wiring end to end       ($0.007)
    profile     Street View coverage + panorama age           (FREE)
    ids         collect dataflik_ids for a cohort             (free)
    score       submit bulk jobs and collect results          ($0.007 each)
    report      Excel review sheet with image links           (free)

WHY `profile` EXISTS AND RUNS FIRST. The Street View METADATA endpoint costs
nothing and returns both whether a panorama exists and when it was taken. Those
two facts decide the whole project before a cent is spent:

  * NO PANORAMA MEANS UNSCOREABLE. Rural Knox and Blount have real coverage gaps,
    and a property with no imagery is invisible to this tool no matter how
    distressed it is. Coverage rate sets the true usable list size.

  * A STALE PANORAMA IS A STALE SCORE. If a neighborhood's imagery is from 2016,
    its scores describe a decade-old condition. Worse, images cache in the
    Lambda's S3 permanently keyed by `dataflik_id`, so a bad-vintage score
    persists until someone deletes the cache. Age is measured per geo and stale
    geos are cut on evidence, before we pay for them.

THE SPEND GUARDS, each from a specific way this could go wrong:

  * A LEDGER of every dataflik_id ever submitted. Re-submitting is re-billing on
    the Gemini side even though S3 caches the image, and it wastes the monthly
    gateway quota either way.
  * `--max-new` (default 25) caps previously-unseen ids per run, so an accidental
    full-county job cannot happen by typo.
  * ONE JOB AT A TIME, polled every 45s rather than the doc's 30s, because polls
    spend the 1,000-requests-per-month gateway quota (about 20 per job).
  * THE JOB ID IS PERSISTED THE INSTANT THE TRIGGER RETURNS. A lost job id is a
    job we paid for and cannot collect.

THE FIRST CALL OF THE DAY ALWAYS 504s. API Gateway's integration timeout is a
hard 29 seconds; their Lambda needs about 45 to load the CLIP model. Measured
live 2026-09-02: attempt 1 died at exactly 29.1s, attempt 2 returned in 5.0s.
`api_post` retries it. Do not read a 504 as a broken key or a bad payload.

THE SCORE RESPONSE CARRIES `neighborhood` AND `year_built`, which search rows do
not. So anything we pay to score comes back already tagged with the two fields
that otherwise cost a slow detail call each.

THREE RESULTS THAT MUST BE COUNTED, NEVER READ AS ZERO. A score of `null` with
`no_panorama` is missing data, not a pristine house. A `photo_quality_score` or
`house_visible_score` under 50 means the image is too poor to assess, so the
score is excluded from ranking rather than trusted. An `ok:false` 400 means the
property is not in their index at all.

GEMINI MODEL NOTE (verified 2026-09-01): our key CANNOT call `gemini-2.5-flash`;
it 404s as "no longer available to new users" while still appearing in the models
listing. Nic's own key works because it is grandfathered. If explanations come
back empty while scores look fine, that is the cause.

    python src/visual_distress.py --phase doctor
    python src/visual_distress.py --phase profile --sample 1000
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config  # noqa: F401  loads .env
    OUTPUT_ROOT = Path(getattr(config, "OUTPUT_DIR", "output"))
except Exception:
    OUTPUT_ROOT = Path("output")

log = logging.getLogger("visual_distress")

OUT = OUTPUT_ROOT / "visual_distress"
LEDGER_PATH = OUT / "ledger.json"
STATE_PATH = OUT / "state.json"

API_BASE = "https://3flwwk1be2.execute-api.us-east-1.amazonaws.com/prod"
SV_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
SV_IMAGE = "https://maps.googleapis.com/maps/api/streetview"

# Their thresholds, on the API's 0-100 scale (the model works in 0-1).
DISTRESS_THRESHOLD = 35     # at or above: distressed, earns an explanation
SEVERE_THRESHOLD = 75       # roughly their top 5%
VALIDITY_FLOOR = 50         # photo_quality / house_visible below this: unusable

POLL_SECONDS = 45
BULK_MAX_IDS = 10_000
IMAGE_COST = 0.007


# ---------------------------------------------------------------- state ----

def _load(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def gate(ok: bool, msg: str) -> None:
    if not ok:
        raise SystemExit("GATE FAILED: " + msg)


def _key(name: str) -> str:
    v = os.getenv(name, "").strip()
    gate(bool(v), "%s is not set; add it to .env" % name)
    return v


# ------------------------------------------------------------ transport ----

def _get_json(url: str, timeout: float = 45.0):
    req = urllib.request.Request(url, headers={"accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def api_post(payload: dict, path: str, timeout: float = 90.0,
             retries: int = 3) -> dict:
    """POST to the Visual Distress gateway, retrying the cold-start timeout.

    THE FIRST CALL AFTER AN IDLE PERIOD ALWAYS FAILS, and it is not our fault.
    API Gateway has a hard 29-second integration timeout while their Lambda
    needs about 45 seconds to load the CLIP model from S3. Measured live
    2026-09-02: attempt 1 returned 504 "Endpoint request timed out" at exactly
    29.1s, attempt 2 returned 200 in 5.0s. So a 504 means "the model is now
    warming, ask again", NOT that the request was wrong, and a client that
    treats it as a failure will look broken every single morning.

    A 400 or 403 is a real answer about a real problem and is never retried.
    """
    body_bytes = json.dumps(payload).encode()
    last = {}
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            API_BASE + path, data=body_bytes, method="POST",
            headers={"content-type": "application/json",
                     "x-api-key": _key("VISUAL_DISTRESS_API_KEY")})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:400]
            try:
                last = json.loads(raw)
            except Exception:
                last = {"ok": False, "error": raw}
            last["_http"] = e.code
            if e.code in (504, 502, 429) and attempt < retries:
                wait = 5.0 * attempt
                log.warning("http %s on %s (cold start), retry %d/%d in %.0fs",
                            e.code, path, attempt, retries, wait)
                time.sleep(wait)
                continue
            return last
        except Exception as e:
            last = {"ok": False, "error": str(e)[:200]}
            if attempt < retries:
                time.sleep(5.0 * attempt)
                continue
            return last
    return last


# --------------------------------------------------------------- ledger ----

def ledger() -> dict:
    return _load(LEDGER_PATH, {})


def ledger_add(entries: dict) -> None:
    led = ledger()
    led.update(entries)
    _save(LEDGER_PATH, led)


# -------------------------------------------------------------- metadata ----

def sv_metadata(lat, lon, key: str, retries: int = 2) -> dict:
    """FREE. Returns status and panorama date without billing an image.

    Retried once, because a transient network error is NOT the same answer as
    ZERO_RESULTS and lumping them together understates coverage. On the first
    1,500-property profile 1.5% came back as transport errors, which would have
    read as 1.5% of the market being unreachable when it is merely a dropped
    connection.
    """
    q = urllib.parse.urlencode({"location": "%s,%s" % (lat, lon), "key": key})
    err = ""
    for attempt in range(1, retries + 1):
        try:
            return _get_json(SV_META + "?" + q, timeout=30)
        except Exception as e:
            err = str(e)[:120]
            if attempt < retries:
                time.sleep(0.6 * attempt)
    return {"status": "REQUEST_ERROR", "_err": err}


# --------------------------------------------------------------- doctor ----

def phase_doctor(args) -> None:
    """One image, and it proves the assumption everything else rests on.

    SiftMap calls the property id `id`; the Lambda looks it up in OpenSearch as
    `dataflik_id`. Every cohort we build assumes those are one identifier space.
    If they are not, we would be scoring the wrong houses and nothing downstream
    would notice, so this asserts the echoed address matches the one we resolved.
    """
    from siftmap_standalone import SiftMapClient

    addr = args.address
    log.info("resolving %r through SiftMap autocomplete", addr)
    hits = SiftMapClient().autocomplete(addr, limit=3)
    gate(bool(hits), "SiftMap returned no candidates for %r" % addr)
    pid = hits[0].get("id") or hits[0].get("dataflik_id")
    resolved = hits[0].get("address") or hits[0].get("title") or ""
    log.info("  -> dataflik_id %s  (%s)", pid, resolved)

    log.info("POST /score (this bills one image, $%.3f)", IMAGE_COST)
    r = api_post({"property_id": int(pid), "include_llm": False}, "/score")

    print()
    print("ok:                    %s" % r.get("ok"))
    print("PropertyID:            %s" % r.get("PropertyID"))
    print("address echoed back:   %s" % r.get("address"))
    print("visual_distress_score: %s" % r.get("visual_distress_score"))
    print("photo_quality / visible: %s / %s"
          % (r.get("photo_quality_score"), r.get("house_visible_score")))
    print("neighborhood:          %s" % r.get("neighborhood"))
    print("errors:                %s" % (r.get("errors") or []))

    gate(r.get("ok") is True, "score call failed: %s" % str(r)[:300])
    echoed = (r.get("address") or "").lower()
    street = resolved.lower().split(",")[0].strip()
    gate(bool(street) and street[:12] in echoed,
         "ID SPACE MISMATCH: asked for %r, got back %r. SiftMap `id` is not the "
         "Lambda's `dataflik_id`." % (resolved, r.get("address")))
    print()
    print("PASS: SiftMap `id` and the Lambda's `dataflik_id` are one space.")

    # Negative controls: assert the failure modes rather than assuming them.
    print()
    print("negative controls:")
    real = os.environ.get("VISUAL_DISTRESS_API_KEY")
    os.environ["VISUAL_DISTRESS_API_KEY"] = "definitely-not-a-valid-key"
    bad = api_post({"property_id": int(pid)}, "/score", timeout=45)
    os.environ["VISUAL_DISTRESS_API_KEY"] = real
    print("  bad key      -> http %s %s" % (bad.get("_http"),
                                            str(bad.get("message") or bad.get("error"))[:60]))
    missing = api_post({"property_id": 999999999}, "/score", timeout=90)
    print("  unknown prop -> http %s ok=%s %s"
          % (missing.get("_http"), missing.get("ok"),
             str(missing.get("error"))[:70]))

    ledger_add({str(pid): {"phase": "doctor", "score": r.get("visual_distress_score"),
                           "when": datetime.now().isoformat(timespec="seconds")}})
    _save(OUT / "doctor_result.json", r)


# -------------------------------------------------------------- profile ----

def phase_profile(args) -> None:
    """FREE Street View coverage and panorama age across the candidate universe.

    Nothing here bills. The metadata endpoint answers "is there an image" and
    "how old is it" for nothing, which is exactly the pair of facts that decides
    how much of this market is reachable and how much of it is worth paying for.
    """
    key = _key("GOOGLE_STREETVIEW_API_KEY")
    rows = _sample_rows(args)
    gate(bool(rows), "no rows to profile; run tn_market_analysis --phase sweep")
    log.info("profiling %s properties (FREE, metadata only)", len(rows))

    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(sv_metadata, r["latitude"], r["longitude"], key): r
                for r in rows}
        for f in as_completed(futs):
            r = futs[f]
            m = f.result()
            results.append({
                "id": r.get("id"), "zip": r.get("zip"),
                "county": r.get("county"), "address": r.get("address"),
                "status": m.get("status"), "pano_date": m.get("date"),
                "value": r.get("estimatedValue"),
            })
            done += 1
            if done % 200 == 0:
                log.info("  %s / %s", done, len(rows))

    _save(OUT / "coverage_profile.json", results)
    _report_coverage(results)


def _sold_ids() -> set:
    """Properties sold arms-length since the cutoff. Never score a recent buyer."""
    p = OUTPUT_ROOT / "tn_market" / "sold_ids.json"
    if not p.exists():
        return set()
    d = json.loads(p.read_text(encoding="utf-8"))
    return {str(x) for x in (d.get("ids") or [])}


def _suppressed_neighborhoods() -> set:
    """Neighborhoods the market analysis cut, so a ZIP filter cannot re-admit them."""
    import csv
    p = OUTPUT_ROOT / "tn_market" / "tn_neighborhood_suppress.csv"
    if not p.exists():
        return set()
    out = set()
    with p.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if (r.get("suppress_reasons") or "").strip():
                out.add(r["geo"])
    return out


def _neighborhood_of() -> dict:
    """dataflik_id -> neighborhood, from the geo assignment pass."""
    out = {}
    p = OUTPUT_ROOT / "tn_market" / "assigned.jsonl"
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            out[str(d.get("id"))] = d.get("neighborhood")
    return out


def _surviving_zips() -> set:
    """ZIPs with no suppression reason, from the market analysis output."""
    import csv
    p = OUTPUT_ROOT / "tn_market" / "tn_zip_suppress.csv"
    if not p.exists():
        return set()
    out = set()
    with p.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not (r.get("suppress_reasons") or "").strip():
                out.add(r["geo"])
    return out


def _sample_rows(args) -> list:
    """A random sample of swept properties that carry usable coordinates."""
    src = OUTPUT_ROOT / "tn_market"
    rows = []
    for name in ("knox", "blount"):
        p = src / ("sweep_%s.jsonl" % name)
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("latitude") and d.get("longitude"):
                    rows.append(d)
    if args.county:
        want = args.county.lower()
        rows = [r for r in rows if (r.get("county") or "").lower() == want]

    if getattr(args, "zips", ""):
        zs = {z.strip() for z in args.zips.split(",") if z.strip()}
        before = len(rows)
        rows = [r for r in rows if r.get("zip") in zs]
        log.info("restricted to %d ZIPs: %d -> %d rows", len(zs), before, len(rows))
        gate(bool(rows), "no rows in ZIPs %s" % sorted(zs))

    # Sample only from ZIPs that survived suppression, because those are the
    # only ones we would ever pay to score. Calibrating on a market we have
    # already ruled out would answer a question nobody asked.
    if getattr(args, "survivors_only", False):
        cutnb = _suppressed_neighborhoods()
        if cutnb:
            nb = _neighborhood_of()
            before = len(rows)
            rows = [r for r in rows if nb.get(str(r.get("id"))) not in cutnb]
            log.info("dropped %d rows in %d suppressed neighborhoods",
                     before - len(rows), len(cutnb))
        keep = _surviving_zips()
        if keep:
            before = len(rows)
            rows = [r for r in rows if r.get("zip") in keep]
            log.info("restricted to %d surviving ZIPs: %d -> %d rows",
                     len(keep), before, len(rows))
        else:
            log.warning("no suppression file found; sampling the whole universe")
    # Entity-held: the CRM row already carries this, so it is exact and free.
    # An LLC or corporate owner is not the door-knock target this list exists
    # to produce.
    if not getattr(args, "no_entity_filter", False):
        before = len(rows)
        rows = [r for r in rows if not r.get("corporateOwned")]
        log.info("entity-held excluded: %d -> %d rows (dropped %d)",
                 before, len(rows), before - len(rows))
        gate(before != len(rows),
             "the entity filter removed nothing, which almost certainly means "
             "corporateOwned is missing from the swept rows")

    # Recently sold: a buyer from last year is not a motivated seller.
    if not getattr(args, "no_sold_filter", False):
        sold = _sold_ids()
        if not sold:
            log.warning("no sold_ids.json; run tn_market_analysis --phase sold")
        else:
            before = len(rows)
            rows = [r for r in rows if str(r.get("id")) not in sold]
            log.info("sold since cutoff excluded: %d -> %d rows (dropped %d)",
                     before, len(rows), before - len(rows))

    # The buy box is a scoring criterion, not just a reporting one: paying to
    # look at a $900K house we would never buy is wasted money.
    lo, hi = args.value_min, args.value_max
    before = len(rows)
    rows = [r for r in rows
            if isinstance(r.get("estimatedValue"), (int, float))
            and lo <= r["estimatedValue"] <= hi]
    log.info("buy box $%s-$%s: %d -> %d rows",
             format(lo, ","), format(hi, ","), before, len(rows))

    random.seed(args.seed)
    random.shuffle(rows)
    # --sample 0 means take everything that survived the filters.
    return rows[:args.sample] if args.sample > 0 else rows


def _report_coverage(results: list) -> None:
    n = len(results)
    by_status = Counter(r["status"] for r in results)
    ok = [r for r in results if r["status"] == "OK"]
    print()
    print("=" * 66)
    print("STREET VIEW COVERAGE  (%s properties, $0.00 spent)" % f"{n:,}")
    print("=" * 66)
    for s, c in by_status.most_common():
        print("  %-18s %6d  %5.1f%%" % (s, c, 100.0 * c / n))
    cov = 100.0 * len(ok) / n if n else 0
    print()
    print("  COVERAGE: %.1f%%  -> %s of every 1,000 properties are scoreable"
          % (cov, int(round(cov * 10))))

    years = [int(r["pano_date"][:4]) for r in ok
             if r.get("pano_date") and len(r["pano_date"]) >= 4]
    if years:
        years.sort()
        cur = datetime.now().year
        print()
        print("PANORAMA AGE (of the %s with imagery)" % f"{len(years):,}")
        for y, c in sorted(Counter(years).items(), reverse=True)[:12]:
            bar = "#" * max(1, int(40.0 * c / len(years)))
            print("  %4d  %5d  %5.1f%%  %s" % (y, c, 100.0 * c / len(years), bar))
        med = years[len(years) // 2]
        stale = sum(1 for y in years if y <= cur - 5)
        print()
        print("  median vintage: %s   |   %.1f%% is 5+ years old"
              % (med, 100.0 * stale / len(years)))
        if stale / len(years) > 0.25:
            print("  WARNING: over a quarter of the imagery is 5+ years old, so "
                  "those scores describe a house as it was, not as it is.")

    per_zip = defaultdict(lambda: [0, 0, []])
    for r in results:
        z = per_zip[r["zip"] or "(blank)"]
        z[1] += 1
        if r["status"] == "OK":
            z[0] += 1
            if r.get("pano_date"):
                z[2].append(int(r["pano_date"][:4]))
    worst = sorted((v[0] / v[1], k, v) for k, v in per_zip.items() if v[1] >= 8)
    if worst:
        print()
        print("WORST-COVERED ZIPs (n>=8 in sample):")
        for frac, z, v in worst[:10]:
            yrs = sorted(v[2])
            med = yrs[len(yrs) // 2] if yrs else "n/a"
            print("  %-10s %5.1f%% covered  n=%-4d median vintage %s"
                  % (z, 100 * frac, v[1], med))
    print()
    print("written: %s" % (OUT / "coverage_profile.json"))


# ------------------------------------------------------------- addresses ----

# SiftMap autocomplete matches on the ABBREVIATED suffix and nothing else.
# Verified live 2026-09-02: "2738 Jefferson Avenue" returns ZERO hits while
# "2738 Jefferson Ave" returns the property. County exports write the suffix out
# in full and in caps, so feeding them straight in silently loses records, which
# is exactly how a ten-property control group became four.
_SUFFIX = {
    "AVENUE": "Ave", "STREET": "St", "ROAD": "Rd", "DRIVE": "Dr",
    "LANE": "Ln", "COURT": "Ct", "CIRCLE": "Cir", "BOULEVARD": "Blvd",
    "PLACE": "Pl", "TERRACE": "Ter", "PARKWAY": "Pkwy", "HIGHWAY": "Hwy",
    "TRAIL": "Trl", "PIKE": "Pike", "WAY": "Way", "SQUARE": "Sq",
    "CROSSING": "Xing", "POINT": "Pt", "RIDGE": "Rdg", "HOLLOW": "Holw",
}


def _abbrev(street: str) -> str:
    out = []
    for w in (street or "").split():
        key = w.strip(",.").upper()
        out.append(_SUFFIX.get(key, w.title() if w.isupper() else w))
    return " ".join(out)


def _addr_variants(street: str, city: str, st: str, zipc: str) -> list:
    """Formats to try, most specific first.

    A ZIP appended without a comma also suppresses matches, so the variants
    deliberately drop it before falling back to the bare street.
    """
    street = _abbrev(street)
    city = (city or "").title()
    st = (st or "").upper()
    out = []
    if city and st:
        out.append("%s, %s, %s" % (street, city, st))
        out.append("%s %s %s" % (street, city, st))
    if city:
        out.append("%s, %s" % (street, city))
    out.append(street)
    seen, uniq = set(), []
    for v in out:
        v = " ".join(v.split())
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def resolve_id(client, street: str, city: str, st: str, zipc: str):
    """Address to dataflik_id, trying each accepted format in turn."""
    for v in _addr_variants(street, city, st, zipc):
        try:
            hits = client.autocomplete(v, limit=2)
        except Exception:
            continue
        if hits:
            pid = str(hits[0].get("id") or "")
            if pid:
                return pid, (hits[0].get("address") or v), v
    return "", "", ""


# ------------------------------------------------------------------ ids ----

CONTROL_CSV = "knox_ftm_current.csv"
CONTROL_TYPES = {"code_violation", "condemned"}


def phase_ids(args) -> None:
    """Assemble the cohort to score: a known-bad control plus a random sample.

    THE CONTROL GROUP IS SMALL AND THAT IS THE REAL CONSTRAINT. The FTM pull
    holds 2,745 Knox rows but only ten are Condemned or code-violation records;
    the other 2,735 are liens, which say nothing whatsoever about the physical
    condition of a house. So calibration cannot lean on volume. It leans on
    those ten being ranked correctly, plus a human reading the top and bottom of
    a random sample. That is the same exercise their model was trained on, just
    at the scale we can afford.
    """
    from siftmap_standalone import SiftMapClient

    args.survivors_only = not args.all_zips
    rows = _sample_rows(args)
    gate(bool(rows), "no swept rows; run tn_market_analysis --phase sweep first")

    control = []
    cpath = OUTPUT_ROOT / CONTROL_CSV
    if cpath.exists():
        import csv
        c = SiftMapClient()
        with cpath.open(encoding="utf-8-sig", errors="replace") as fh:
            for r in csv.DictReader(fh):
                nt = (r.get("Notice Type") or "").strip().lower()
                lst = (r.get("Lists") or "").strip().lower()
                if nt not in CONTROL_TYPES and "condemned" not in lst:
                    continue
                pid, matched, used = resolve_id(
                    c, r.get("Property Street Address"), r.get("Property City"),
                    r.get("Property State"), r.get("Property ZIP Code"))
                if not pid:
                    log.warning("control address did not resolve: %s",
                                r.get("Property Street Address"))
                    continue
                control.append({"id": pid, "address": matched,
                                "matched_via": used,
                                "cohort": "control_condemned"})
        log.info("resolved %d known-bad control properties", len(control))
    else:
        log.warning("%s not found; no control group", cpath)

    ctrl_ids = {c["id"] for c in control}
    sample = [{"id": str(r["id"]), "address": r.get("address"),
               "zip": r.get("zip"), "county": r.get("county"),
               "value": r.get("estimatedValue"), "cohort": "random"}
              for r in rows if str(r.get("id")) not in ctrl_ids]
    if args.sample > 0:
        sample = sample[:args.sample]

    cohort = control + sample
    _save(OUT / "cohort.json", cohort)
    print()
    print("cohort: %d control + %d random = %d total"
          % (len(control), len(sample), len(cohort)))
    print("estimated cost if all are new: $%.2f" % (len(cohort) * IMAGE_COST))
    print("written: %s" % (OUT / "cohort.json"))


# ---------------------------------------------------------------- score ----

def phase_score(args) -> None:
    """Submit one bulk job and collect it, with the spend guards on.

    Every guard here exists because of a specific way this could go wrong, and
    they are cheap compared to what they prevent.
    """
    cohort = _load(OUT / "cohort.json", [])
    gate(bool(cohort), "no cohort.json; run --phase ids first")

    led = ledger()
    fresh = [c for c in cohort if str(c["id"]) not in led]
    already = len(cohort) - len(fresh)
    log.info("cohort %d | already scored %d | new %d",
             len(cohort), already, len(fresh))

    if not fresh:
        print("nothing new to score; every id is already in the ledger.")
        return

    # --batch is the DELIBERATE "submit this many now" lever; --max-new stays the
    # accidental-spend guard. Keeping them separate means raising the batch size
    # is an explicit act and never a side effect of loosening a safety check.
    if args.batch > 0 and len(fresh) > args.batch:
        log.info("batching: %d fresh, submitting %d this run", len(fresh), args.batch)
        fresh = fresh[:args.batch]

    # A typo must never be able to launch a full-county job.
    if len(fresh) > args.max_new:
        gate(False,
             "%d new ids exceeds --max-new %d. Raise it DELIBERATELY if that "
             "is really the spend you intend ($%.2f)."
             % (len(fresh), args.max_new, len(fresh) * IMAGE_COST))
    gate(len(fresh) <= BULK_MAX_IDS,
         "bulk accepts at most %d ids per job" % BULK_MAX_IDS)

    cost = len(fresh) * IMAGE_COST
    if not args.commit:
        print()
        print("DRY RUN. Would submit %d new properties, about $%.2f."
              % (len(fresh), cost))
        print("Re-run with --commit to actually spend it.")
        return

    ids = [int(c["id"]) for c in fresh]

    # COOL-OFF BETWEEN JOBS. Four back-to-back 10,000-property jobs degraded
    # from 2.9% to 99.5% "no panorama" as something upstream ran out of rate.
    # Waiting between jobs costs minutes; not waiting cost ~11,000 wasted
    # scores. Set --cool-off 0 deliberately if a burst is really wanted.
    jobs_prev = _load(OUT / "jobs.json", {})
    if args.cool_off > 0 and jobs_prev:
        last = max((v.get("submitted", "") for v in jobs_prev.values()),
                   default="")
        if last:
            try:
                age = (datetime.now()
                       - datetime.fromisoformat(last)).total_seconds()
            except Exception:
                age = 1e9
            if age < args.cool_off:
                wait = args.cool_off - age
                log.info("cooling off %.0fs since the last job before "
                         "submitting (--cool-off)", wait)
                time.sleep(wait)

    log.info("triggering bulk job for %d properties (about $%.2f)", len(ids), cost)
    r = api_post({"action": "trigger", "property_ids": ids}, "/bulk")
    job = r.get("job_id")
    gate(bool(job), "trigger returned no job_id: %s" % str(r)[:250])

    # PERSIST THE JOB ID BEFORE ANYTHING ELSE. A job id lost between the
    # trigger and the first poll is a job we have paid for and cannot collect.
    jobs = _load(OUT / "jobs.json", {})
    jobs[job] = {"submitted": datetime.now().isoformat(timespec="seconds"),
                 "n": len(ids), "est_cost": round(cost, 2), "status": "running"}
    _save(OUT / "jobs.json", jobs)
    # The ids THIS job carried, so a later --phase collect reconciles against
    # the right 10,000 and not the whole cohort. Without this the collect path
    # compared 9,999 returned against a 38,696-row cohort and reported 28,697
    # "paid for, not received", which is a false alarm loud enough to make a
    # real one unreadable.
    _save(OUT / ("submitted_%s.json" % job), [str(i) for i in ids])
    log.info("job_id %s persisted (%d ids)", job, len(ids))

    deadline = time.time() + args.timeout
    status = "running"
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        st = api_post({"action": "status", "job_id": job}, "/bulk")
        status = st.get("status", "unknown")
        log.info("  job %s: %s", job, status)
        if status in ("completed", "failed", "not_found"):
            break
    jobs[job]["status"] = status
    _save(OUT / "jobs.json", jobs)
    gate(status == "completed",
         "job %s ended as %s (%s). The job id is saved in jobs.json; collect it "
         "later with --phase collect." % (job, status, st.get("failure_reason")))

    res = api_post({"action": "result", "job_id": job}, "/bulk")
    _collect(job, res, fresh)


def phase_collect(args) -> None:
    """Fetch a job we already paid for. Separate so a dropped poll costs nothing."""
    jobs = _load(OUT / "jobs.json", {})
    job = args.job_id or next((j for j, v in jobs.items()
                               if v.get("status") != "collected"), "")
    gate(bool(job), "no job to collect; pass --job-id")
    res = api_post({"action": "result", "job_id": job}, "/bulk")
    gate(res.get("status") == "completed",
         "job %s is %s, not collected" % (job, res.get("status")))
    cohort = {str(c["id"]): c for c in _load(OUT / "cohort.json", [])}
    sent = _load(OUT / ("submitted_%s.json" % job), None)
    if sent:
        submitted = [cohort.get(i) or {"id": i} for i in sent]
    else:
        log.warning("no submitted_%s.json; reconciling against the whole "
                    "cohort, so the missing count will overstate", job)
        submitted = list(cohort.values())
    _collect(job, res, submitted)


def _collect(job: str, res: dict, submitted: list) -> None:
    url = res.get("result_url")
    gate(bool(url), "no result_url on a completed job: %s" % str(res)[:250])
    log.info("downloading results (presigned, expires in %ss)", res.get("expires_in"))
    with urllib.request.urlopen(url, timeout=180) as r:
        raw = r.read().decode("utf-8", "replace")

    path = OUT / ("job_%s.jsonl" % job)
    path.write_text(raw, encoding="utf-8")
    results = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                results.append(json.loads(line))
            except Exception:
                continue
    log.info("collected %d result rows into %s", len(results), path)

    meta = {str(c["id"]): c for c in submitted}

    # RECONCILE RETURNED IDS AGAINST SUBMITTED IDS. Partial-failure behaviour on
    # this endpoint is undocumented, so a job that quietly drops rows would
    # otherwise read as a clean success and we would never know which properties
    # we paid for and never got. Same class of check that caught a sweep
    # reporting 144,319 of 153,757 rows as DONE.
    sent = {str(c["id"]) for c in submitted}
    got = {str(d.get("PropertyID")) for d in results if d.get("PropertyID")}
    missing = sent - got
    extra = got - sent
    log.info("reconcile: submitted %d, returned %d, missing %d, unexpected %d",
             len(sent), len(got), len(missing), len(extra))
    if missing:
        _save(OUT / ("missing_%s.json" % job), sorted(missing))
        log.warning("%d submitted ids did not come back; written to missing_%s.json",
                    len(missing), job)
    if extra:
        log.warning("%d returned ids were never submitted", len(extra))

    now = datetime.now().isoformat(timespec="seconds")
    entries = {}
    for d in results:
        pid = str(d.get("PropertyID") or "")
        if pid:
            entries[pid] = {"job": job, "when": now,
                            "score": d.get("visual_distress_score"),
                            "cohort": (meta.get(pid) or {}).get("cohort", "")}
    ledger_add(entries)

    jobs = _load(OUT / "jobs.json", {})
    if job in jobs:
        jobs[job]["status"] = "collected"
        jobs[job]["returned"] = len(results)
        _save(OUT / "jobs.json", jobs)

    _summarise(results, meta)


def _summarise(results: list, meta: dict) -> None:
    """Read the results honestly. Missing is not zero and unusable is not clean."""
    n = len(results)
    scored, no_pano, unusable, errored = [], [], [], []
    for d in results:
        # THE BULK JSONL HAS NO `ok` FIELD. Only the synchronous /score response
        # carries one. A first version gated on `ok is not True` and duly
        # reported all 249 perfectly good rows as errors, which is the exact
        # read-a-success-as-a-failure bug this file is meant to avoid. A row is
        # a failure only if it says so, or if it carries no property id at all.
        if d.get("ok") is False or d.get("error") or not d.get("PropertyID"):
            errored.append(d)
            continue
        sc = d.get("visual_distress_score")
        if sc is None:
            no_pano.append(d)
            continue
        pq = d.get("photo_quality_score") or 0
        hv = d.get("house_visible_score") or 0
        if pq < VALIDITY_FLOOR or hv < VALIDITY_FLOOR:
            unusable.append(d)
            continue
        scored.append(d)

    print()
    print("=" * 66)
    print("RESULTS  (%d properties returned)" % n)
    print("=" * 66)
    if meta:
        miss = len(set(meta) - {str(d.get("PropertyID")) for d in results})
        if miss:
            print("  SUBMITTED BUT NOT RETURNED  %5d  <- paid for, not received"
                  % miss)
    print("  usable scores           %5d  %5.1f%%" % (len(scored), 100.0 * len(scored) / n if n else 0))
    print("  no panorama             %5d   (missing data, NOT a clean house)" % len(no_pano))
    print("  image too poor to judge %5d   (excluded from ranking)" % len(unusable))
    print("  errored                 %5d" % len(errored))
    if not scored:
        return

    vals = sorted(d["visual_distress_score"] for d in scored)
    print()
    print("  score distribution: min %d  p25 %d  median %d  p75 %d  max %d"
          % (vals[0], vals[len(vals) // 4], vals[len(vals) // 2],
             vals[3 * len(vals) // 4], vals[-1]))
    print("  at or above %d (distressed): %d  |  at or above %d (severe): %d"
          % (DISTRESS_THRESHOLD,
             sum(1 for v in vals if v >= DISTRESS_THRESHOLD),
             SEVERE_THRESHOLD,
             sum(1 for v in vals if v >= SEVERE_THRESHOLD)))

    # THE CALIBRATION GATE. Known-bad properties must rank near the top. If they
    # do not, the score does not read East Tennessee stock and nothing further
    # should be bought on the strength of it.
    ranked = sorted(scored, key=lambda d: -d["visual_distress_score"])
    ctrl = [(i + 1, d) for i, d in enumerate(ranked)
            if (meta.get(str(d.get("PropertyID"))) or {}).get("cohort")
            == "control_condemned"]
    if ctrl:
        print()
        print("  KNOWN-BAD CONTROL, where each ranked out of %d:" % len(ranked))
        for rank, d in ctrl:
            print("    #%-4d score %-4s  %s"
                  % (rank, d.get("visual_distress_score"),
                     str(d.get("address"))[:52]))
        top_q = sum(1 for rank, _ in ctrl if rank <= max(1, len(ranked) // 4))
        print()
        print("  %d of %d condemned properties landed in the top quartile."
              % (top_q, len(ctrl)))
        if top_q < len(ctrl) * 0.6:
            print("  GATE CONCERN: the control group is NOT sorting to the top. "
                  "Do not buy volume on this score until that is understood.")


# -------------------------------------------------------------- requeue ----

def phase_requeue(args) -> None:
    """Find rows that FAILED rather than genuinely lacking imagery, and retry them.

    THE BULK ENDPOINT DEGRADES UNDER SUSTAINED LOAD, and it fails quietly. Four
    back-to-back 10,000-property jobs measured 2.9%, 2.8%, 42.6%, 99.5% and
    99.2% "no panorama". A resource ran out mid-run: the same Street View key
    tested clean immediately afterwards, returning a real 98KB JPEG, so it was a
    rate ceiling that recovered, not a dead key or a billing wall.

    THE TWO CASES LOOK IDENTICAL IN THE RESULT ROW. A property with no Street
    View coverage and a property whose fetch failed both come back with a null
    score. The free METADATA endpoint separates them: it costs nothing and says
    whether imagery exists. Measured on 300 failed rows, 96% had imagery, so
    they were failures, not gaps.

    A FAILED SCORE MUST NOT STAY IN THE LEDGER AS SCORED. The ledger exists to
    prevent re-billing, but a null score that was really a timeout is not a
    result. Those ids are released here so a retry can pick them up; genuine
    ZERO_RESULTS properties stay recorded so we never pay to look at a house
    Google has never photographed.
    """
    key = _key("GOOGLE_STREETVIEW_API_KEY")
    files = sorted(OUT.glob("job_*.jsonl"))
    gate(bool(files), "no job files in %s" % OUT)

    failed = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("visual_distress_score") is None and d.get("lat"):
                    failed.append(d)
    if not failed:
        print("no null-score rows to check.")
        return
    log.info("checking %d null-score rows against the FREE metadata endpoint",
             len(failed))

    retryable, genuine = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(sv_metadata, d["lat"], d["lon"], key): d
                for d in failed}
        for fut in as_completed(futs):
            d = futs[fut]
            st = (fut.result() or {}).get("status")
            (retryable if st == "OK" else genuine).append(d)
            done += 1
            if done % 1000 == 0:
                log.info("  %d / %d", done, len(failed))

    print()
    print("null-score rows          %6d" % len(failed))
    print("  imagery EXISTS (retry) %6d  %.1f%%"
          % (len(retryable), 100.0 * len(retryable) / len(failed)))
    print("  genuinely no coverage  %6d" % len(genuine))
    print("  cost to re-score       $%.2f" % (len(retryable) * IMAGE_COST))

    if not args.commit:
        print()
        print("DRY RUN. Re-run with --commit to release these ids for retry.")
        return

    # Release the retryable ids from the ledger and strip their dead rows from
    # the job files, so the next `score` run picks them up naturally.
    ids = {str(d["PropertyID"]) for d in retryable}
    led = ledger()
    for i in ids:
        led.pop(i, None)
    _save(LEDGER_PATH, led)

    stripped = 0
    for f in files:
        rows = []
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if str(d.get("PropertyID")) in ids:
                    stripped += 1
                    continue
                rows.append(d)
        tmp = f.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for d in rows:
                fh.write(json.dumps(d, default=str) + "\n")
        tmp.replace(f)

    print()
    print("released %s ids from the ledger, stripped %s dead rows"
          % (format(len(ids), ","), format(stripped, ",")))
    print("re-run --phase ids then --phase score to pick them up")


# ---------------------------------------------------------------- purge ----

def phase_purge(args) -> None:
    """Remove already-scored rows that the new exclusion rules reject.

    These were paid for before the rules existed. Ty's call is to delete them
    rather than carry them flagged, so the job files are rewritten without them.

    THE LEDGER KEEPS THEIR IDS, MARKED PURGED, AND THAT IS NOT A HEDGE. The
    ledger is the only thing preventing a later run from re-submitting and
    re-billing these exact properties. Removing them from the ledger as well
    would mean paying a second time for houses we just decided we do not want.

    Dry by default. This destroys paid-for data, so it takes --commit.
    """
    sold = _sold_ids()
    idx = _sweep_index()
    if not sold:
        log.warning("no sold_ids.json; only the entity rule will apply")

    files = sorted(OUT.glob("job_*.jsonl"))
    gate(bool(files), "no job files in %s" % OUT)

    plan = []
    for f in files:
        rows = []
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
        keep, drop = [], []
        for d in rows:
            pid = str(d.get("PropertyID") or "")
            m = idx.get(pid, {})
            why = ""
            if m.get("corporateOwned"):
                why = "entity-held"
            elif pid in sold:
                why = "sold since cutoff"
            (drop if why else keep).append((d, why))
        plan.append((f, keep, drop))

    total_drop = sum(len(d) for _, _, d in plan)
    total_keep = sum(len(k) for _, k, _ in plan)
    reasons = Counter(w for _, _, d in plan for _, w in d)

    print()
    print("purge plan:")
    for f, keep, drop in plan:
        print("  %-46s keep %6d  drop %5d" % (f.name[:46], len(keep), len(drop)))
    print()
    print("  total keep %s | total drop %s" % (format(total_keep, ","),
                                               format(total_drop, ",")))
    for r, c in reasons.most_common():
        print("    %-22s %6d" % (r, c))
    print("  sunk cost on dropped rows: $%.2f" % (total_drop * IMAGE_COST))

    if not args.commit:
        print()
        print("DRY RUN. Re-run with --commit to rewrite the job files.")
        return

    led = ledger()
    now = datetime.now().isoformat(timespec="seconds")
    for f, keep, drop in plan:
        tmp = f.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for d, _ in keep:
                fh.write(json.dumps(d, default=str) + "\n")
        tmp.replace(f)
        for d, why in drop:
            pid = str(d.get("PropertyID"))
            e = led.get(pid, {})
            e.update({"purged": True, "purge_reason": why, "purged_at": now})
            led[pid] = e
    _save(LEDGER_PATH, led)

    purged = sum(1 for v in led.values() if v.get("purged"))
    print()
    print("rewrote %d job files, dropped %s rows" % (len(plan),
                                                     format(total_drop, ",")))
    print("ledger retains %s purged ids so they are never re-billed" %
          format(purged, ","))


# --------------------------------------------------------------- report ----

# The four canned strings their pipeline substitutes when the LLM does not run.
# THESE ARE NOT ASSESSMENTS. When Gemini is rate-limited the pipeline does not
# leave the field empty, it fills in boilerplate, and the no-distress variant
# then sits on properties the same model scored 100. Measured 2026-09-03: 112
# real generations against 17,996 rows carrying explanation text, and 1,910
# properties scoring 35+ served "shows no clear signs of physical distress".
# A blank is honest; a confident sentence arguing with its own score is not.
_CANNED = (
    "does not provide sufficient visibility",
    "appears to be in excellent condition",
    "shows no clear signs of physical distress",
    "street view image to generate the description is missing",
)


def real_explanation(text) -> str:
    """The model's own words, or '' when the pipeline substituted boilerplate."""
    t = (text or "").strip()
    if not t or any(c in t for c in _CANNED):
        return ""
    return t


COMPONENTS = ["overall_bad", "abandoned", "bad_exterior", "aged_outdated",
              "damaged_windows", "overgrown", "bad_roof",
              "bad_driveways_walkways", "cluttered_yard"]


def _all_results() -> list:
    """Every scored row from every collected job, each tagged with its job.

    POOLING JOBS SILENTLY MIXES COHORTS. The 250-property calibration batch
    sampled across all 33 surviving ZIPs to test the model, so pooling it with a
    top-5-ZIP production run put 170 out-of-scope addresses in the call list with
    nothing to distinguish them. They are real scores and worth keeping, but they
    have to declare themselves.
    """
    out, seen = [], set()
    for p in sorted(OUT.glob("job_*.jsonl")):
        job = p.stem.replace("job_", "")
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                pid = str(d.get("PropertyID") or "")
                if pid and pid not in seen:
                    seen.add(pid)
                    d["_job"] = job
                    out.append(d)
    return out


def _sweep_index() -> dict:
    """dataflik_id -> the market attributes a caller needs alongside the score."""
    idx = {}
    src = OUTPUT_ROOT / "tn_market"
    for name in ("assigned.jsonl", "sweep_knox.jsonl", "sweep_blount.jsonl"):
        p = src / name
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                k = str(d.get("id"))
                cur = idx.setdefault(k, {})
                for f in ("zip", "county", "estimatedValue", "equityPercent",
                          "absenteeOwner", "corporateOwned", "squareFeet",
                          "bedrooms", "bathrooms", "neighborhood", "year_built",
                          "saved_uuid", "distress_active", "address"):
                    if d.get(f) not in (None, "") and f not in cur:
                        cur[f] = d[f]
    return idx


def phase_report(args) -> None:
    """The call list: what a caller works, and what a human must check first."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from lender_package import CALC_FILL, MONEY, TOTAL_FILL, _band, _polish

    res = _all_results()
    gate(bool(res), "no collected job results in %s" % OUT)
    idx = _sweep_index()

    want = {z.strip() for z in (args.zips or "").split(",") if z.strip()}
    if want:
        before = len(res)
        res = [d for d in res
               if (idx.get(str(d.get("PropertyID"))) or {}).get("zip") in want]
        log.info("report restricted to %d ZIPs: %d -> %d rows",
                 len(want), before, len(res))
        gate(bool(res), "no scored rows in ZIPs %s" % sorted(want))
    log.info("%d scored rows, %d indexed market rows", len(res), len(idx))

    scored, no_pano, unusable, errored = [], [], [], []
    for d in res:
        if d.get("ok") is False or d.get("error") or not d.get("PropertyID"):
            errored.append(d)
            continue
        if d.get("visual_distress_score") is None:
            no_pano.append(d)
            continue
        pq = d.get("photo_quality_score") or 0
        hv = d.get("house_visible_score") or 0
        if pq < VALIDITY_FLOOR or hv < VALIDITY_FLOOR:
            unusable.append(d)
            continue
        scored.append(d)
    scored.sort(key=lambda d: -d["visual_distress_score"])

    vals = sorted(d["visual_distress_score"] for d in scored)

    def pct(q):
        if not vals:
            return 0
        return vals[min(len(vals) - 1, int(len(vals) * q / 100.0))]

    wb = Workbook()

    # ---- Call list -------------------------------------------------------
    ws = wb.active
    ws.title = "Call list"
    # The nine component scores are the WHY behind the headline number, and a
    # reviewer needs them: a 78 driven by `abandoned` is a different property
    # from a 78 driven by `aged_outdated`, and only one of those is a lead.
    heads = ["Rank", "Score", "VERIFY", "Address", "ZIP", "Neighborhood",
             "Cohort",
             "Est value", "Equity %", "Owner", "Year", "Beds", "Baths", "Sqft",
             "Known distress",
             "Abandoned", "Overall bad", "Exterior", "Roof", "Windows",
             "Overgrown", "Cluttered yard", "Drives/walks", "Aged/outdated",
             "Visible", "Photo", "What the model saw",
             "Street View", "CRM record", "PropertyID"]
    for i, h in enumerate(heads, 1):
        ws.cell(row=1, column=i, value=h).font = Font(bold=True)
    amber = PatternFill("solid", fgColor="FFF2CC")
    rr = 2
    for rank, d in enumerate(scored, 1):
        m = idx.get(str(d["PropertyID"]), {})
        hv = d.get("house_visible_score") or 0
        # A low-visibility row is not a clean house and not a bad one: it is a
        # row a person has to look at. Flagged, never silently trusted and never
        # silently dropped. Both condemned properties the calibration missed
        # were deep setbacks the camera could not see.
        verify = "LOOK" if hv < 80 else ""
        owner = ("Company" if m.get("corporateOwned") else
                 "Absentee" if m.get("absenteeOwner") else "Owner-occ")
        flags = ", ".join(x.replace("is_", "").replace("_", " ")
                          for x in (m.get("distress_active") or [])[:3])
        # Name the batch this row came from so a pooled file is still readable.
        cohort = ("calibration" if d.get("_job", "").startswith("20260903-0124")
                  else "top5")
        uuid = m.get("saved_uuid")
        lat, lon = d.get("lat"), d.get("lon")
        sv = ("https://www.google.com/maps/@?api=1&map_action=pano"
              "&viewpoint=%s,%s" % (lat, lon)) if lat and lon else d.get("image_url")
        vals_row = [rank, d["visual_distress_score"], verify,
                    d.get("address") or m.get("address"), m.get("zip"),
                    d.get("neighborhood") or m.get("neighborhood"),
                    cohort,
                    m.get("estimatedValue"), m.get("equityPercent"), owner,
                    d.get("year_built") or m.get("year_built"),
                    m.get("bedrooms"), m.get("bathrooms"), m.get("squareFeet"),
                    flags,
                    d.get("abandoned_score"), d.get("overall_bad_score"),
                    d.get("bad_exterior_score"), d.get("bad_roof_score"),
                    d.get("damaged_windows_score"), d.get("overgrown_score"),
                    d.get("cluttered_yard_score"),
                    d.get("bad_driveways_walkways_score"),
                    d.get("aged_outdated_score"),
                    hv, d.get("photo_quality_score"),
                    real_explanation(d.get("distress_explanation"))[:300]
                    or "no description generated",
                    sv,
                    ("https://app.reisift.io/records/%s" % uuid) if uuid else "",
                    d.get("PropertyID")]
        for i, v in enumerate(vals_row, 1):
            c = ws.cell(row=rr, column=i, value=v)
            if i == 7 and v:
                c.number_format = MONEY
            if verify:
                c.fill = amber
        # Clickable rather than a pasted string: this sheet is worked, not read.
        for col, label in ((28, "view"), (29, "open")):
            cell = ws.cell(row=rr, column=col)
            if cell.value:
                cell.hyperlink = cell.value
                cell.value = label
                cell.style = "Hyperlink"
        rr += 1
    ws.freeze_panes = "D2"
    for col, w in (("A", 6), ("B", 7), ("C", 8), ("D", 34), ("E", 8),
                   ("F", 26), ("G", 12), ("H", 12), ("I", 9), ("J", 11),
                   ("K", 7), ("L", 6), ("M", 7), ("N", 8), ("O", 24),
                   ("P", 10), ("Q", 11), ("R", 9), ("S", 7), ("T", 9),
                   ("U", 10), ("V", 13), ("W", 12), ("X", 13),
                   ("Y", 8), ("Z", 7), ("AA", 60), ("AB", 8), ("AC", 11),
                   ("AD", 12)):
        ws.column_dimensions[col].width = w
    ws.auto_filter.ref = "A1:AD%d" % (rr - 1)

    # ---- Distribution ----------------------------------------------------
    ws2 = wb.create_sheet("Distribution")
    r = 1
    ws2.cell(row=r, column=1, value="Score distribution and where to cut").font = \
        Font(bold=True, size=13)
    r += 2
    _band(ws2, r, "What came back", span=3)
    r += 1
    for label, val, note in (
        ("Properties returned", len(res), ""),
        ("Usable scores", len(scored), "the ranking above"),
        ("No panorama", len(no_pano), "missing data, NOT a clean house"),
        ("Image too poor to judge", len(unusable),
         "photo quality or visibility under %d" % VALIDITY_FLOOR),
        ("Errored", len(errored), ""),
    ):
        ws2.cell(row=r, column=1, value=label)
        ws2.cell(row=r, column=2, value=val).fill = CALC_FILL
        if note:
            ws2.cell(row=r, column=3, value=note).font = Font(size=9, italic=True)
        r += 1
    r += 1

    _band(ws2, r, "Percentiles, measured on this cohort", span=3)
    r += 1
    for q in (10, 25, 50, 75, 90, 95, 99):
        ws2.cell(row=r, column=1, value="p%d" % q)
        ws2.cell(row=r, column=2, value=pct(q)).fill = CALC_FILL
        r += 1
    r += 1

    _band(ws2, r, "Where a cut line lands", span=3)
    r += 1
    ws2.cell(row=r, column=1, value="Threshold").font = Font(bold=True)
    ws2.cell(row=r, column=2, value="Properties").font = Font(bold=True)
    ws2.cell(row=r, column=3, value="% of usable").font = Font(bold=True)
    r += 1
    for t in (35, 50, 60, 70, 75, 85, 90):
        n = sum(1 for v in vals if v >= t)
        ws2.cell(row=r, column=1, value=t)
        ws2.cell(row=r, column=2, value=n).fill = CALC_FILL
        ws2.cell(row=r, column=3,
                 value=round(100.0 * n / len(vals), 1) if vals else 0)
        r += 1
    r += 1
    ws2.cell(row=r, column=1, value=(
        "35 and 75 are THEIR thresholds, derived from a national training set. "
        "This table exists so the cut line is chosen from the Knox distribution "
        "actually observed rather than inherited. Pick the row that gives a "
        "list a caller can finish."))
    ws2.cell(row=r, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    ws2.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    ws2.row_dimensions[r].height = 46
    ws2.column_dimensions["A"].width = 26
    ws2.column_dimensions["B"].width = 14
    ws2.column_dimensions["C"].width = 46

    # ---- Excluded --------------------------------------------------------
    ws3 = wb.create_sheet("Excluded")
    for i, h in enumerate(["Why", "PropertyID", "Address", "Score",
                           "Photo", "Visible", "Note"], 1):
        ws3.cell(row=1, column=i, value=h).font = Font(bold=True)
    rr = 2
    for why, group in (("no panorama", no_pano),
                       ("image too poor", unusable), ("error", errored)):
        for d in group:
            m = idx.get(str(d.get("PropertyID")), {})
            for i, v in enumerate([why, d.get("PropertyID"),
                                   d.get("address") or m.get("address"),
                                   d.get("visual_distress_score"),
                                   d.get("photo_quality_score"),
                                   d.get("house_visible_score"),
                                   "; ".join(d.get("errors") or [])[:120]], 1):
                ws3.cell(row=rr, column=i, value=v)
            rr += 1
    ws3.freeze_panes = "A2"
    ws3.column_dimensions["A"].width = 16
    ws3.column_dimensions["C"].width = 36
    ws3.column_dimensions["G"].width = 44

    # ---- Caveats ---------------------------------------------------------
    ws4 = wb.create_sheet("Sources and caveats")
    look = sum(1 for d in scored if (d.get("house_visible_score") or 0) < 80)
    nodesc = sum(1 for d in scored
                 if not real_explanation(d.get("distress_explanation")))
    notes = [
        ("Scoring engine",
         "Dataflik Visual Distress API. CLIP ViT-L/14-336 embedding scored by a "
         "linear model trained on ~2,400 hand-labelled US single-family images."),
        ("Calibration on our own stock",
         "7 of 9 known-condemned Knox properties ranked in the top 11 of 190 on "
         "a 250-property test, against a random-population median of 5."),
        ("VERIFY column",
         "%d rows (%.1f%%) have house_visible under 80. Their scores are "
         "unreliable in BOTH directions, not just low. The two condemned "
         "properties the calibration missed were deep-setback houses the camera "
         "could not see, scoring 29 and 18. Treat a VERIFY row as unread."
         % (look, 100.0 * look / len(scored) if scored else 0)),
        ("No panorama is not a clean house",
         "%d properties have no Street View imagery at all and are on the "
         "Excluded sheet. They are unscored, not unblemished." % len(no_pano)),
        ("Descriptions",
         "%d of %d scored rows (%.0f%%) show 'no description generated'. Their "
         "pipeline substitutes canned boilerplate when the LLM is rate-limited, "
         "and the no-distress variant was landing on properties it had scored "
         "100, so that text is suppressed here rather than shown. The SCORE is "
         "unaffected: it comes from the image model with no LLM involved."
         % (nodesc, len(scored), 100.0 * nodesc / len(scored) if scored else 0)),
        ("Thresholds",
         "The 35 and 75 cut lines in their documentation come from a national "
         "training distribution. Use the Distribution sheet to set ours."),
        ("Street View vintage",
         "Measured free across 1,500 Knox properties: 89.9% have imagery, "
         "median vintage 2026, only 10.4% is five or more years old."),
        ("Cleaning rules applied",
         "Two exclusions, both added after the first batch. ENTITY-HELD: any "
         "property whose owner of record is a company is removed, read from "
         "corporateOwned on the CRM row, which is exact. RECENTLY SOLD: any "
         "arms-length sale on or after 2023-01-01 for $1,000 or more is "
         "removed. Interfamily transfers and nominal deeds are deliberately "
         "KEPT, because a quitclaim to an heir is an inherited property, not a "
         "market sale, and those are among the better lead types here."),
        ("Sold-set completeness",
         "The sold set is swept one calendar quarter at a time rather than as "
         "one three-year window. A single window returned 75.9% of Knox, "
         "because offset pagination repeats rows at depth; quarterly slices are "
         "5 to 7 pages each and return 100%. 31,098 sold ids captured against "
         "31,098 reported across both counties."),
        ("Already-scored rows removed",
         "2,634 previously scored properties failed the new rules (1,362 sold, "
         "1,272 entity-held) and were deleted from the result files. Their ids "
         "remain in the ledger marked purged so they can never be re-submitted "
         "and re-billed. Sunk cost on those rows was $18.44."),
        ("Cohort",
         "Top five Knox ZIPs by investor transactions (37920, 37918, 37921, "
         "37917, 37914), in the $1 to $700K buy box, after ZIP and "
         "neighborhood suppression. The 170 rows from the earlier 250-property "
         "calibration sample are pooled in as well and sit OUTSIDE those five "
         "ZIPs; they are real scores and are kept rather than discarded, but "
         "the ZIP column is the way to tell them apart."),
        ("Distribution is cohort-specific",
         "The calibration sample across all 33 surviving ZIPs had a median of 5 "
         "and p75 of 21. This cohort has a median of 22 and p75 of 48. That is "
         "not drift: the top five investor ZIPs are the older working-class "
         "areas and genuinely carry more visible distress. A threshold carried "
         "over from one cohort to another will be wrong."),
    ]
    for i, (a, b) in enumerate(notes, 1):
        ws4.cell(row=i, column=1, value=a).font = Font(bold=True)
        c = ws4.cell(row=i, column=2, value=b)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws4.row_dimensions[i].height = 44
    ws4.column_dimensions["A"].width = 26
    ws4.column_dimensions["B"].width = 96

    final = (Path(args.out) if args.out else
             OUTPUT_ROOT / ("Knox_Visual_Distress_%s.xlsx"
                            % datetime.now().strftime("%Y%m%d")))
    # Excel holds an EXCLUSIVE lock on an open workbook, so writing to a pending
    # name and swapping protects the save but not the replace. If the target is
    # open, say so plainly and leave the finished file under its pending name
    # rather than throwing a traceback over work that actually succeeded.
    pending = final.with_name("_PENDING_" + final.name)
    wb.save(pending)
    try:
        if final.exists():
            final.unlink()
        pending.replace(final)
    except PermissionError:
        print()
        print("!! %s is OPEN IN EXCEL, so it could not be replaced." % final.name)
        print("   The new workbook is complete and saved as: %s" % pending)
        print("   Close Excel and re-run --phase report, or just open that file.")
        final = pending

    print()
    print("scored %d | no panorama %d | too poor %d | errored %d"
          % (len(scored), len(no_pano), len(unusable), len(errored)))
    print("at or above 75: %d    at or above 50: %d    at or above 35: %d"
          % (sum(1 for v in vals if v >= 75), sum(1 for v in vals if v >= 50),
             sum(1 for v in vals if v >= 35)))
    print("flagged VERIFY (visibility under 80): %d" % look)
    print("written: %s" % final)


# --------------------------------------------------------------- master ----

def _cleaned_universe() -> list:
    """Every property that survives the full exclusion stack, with its geo."""
    import csv as _csv
    src = OUTPUT_ROOT / "tn_market"
    nbcut, zipkeep = set(), set()
    p = src / "tn_neighborhood_suppress.csv"
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                if (r.get("suppress_reasons") or "").strip():
                    nbcut.add(r["geo"])
    p = src / "tn_zip_suppress.csv"
    if p.exists():
        with p.open(encoding="utf-8") as fh:
            for r in _csv.DictReader(fh):
                if not (r.get("suppress_reasons") or "").strip():
                    zipkeep.add(r["geo"])
    sold = _sold_ids()
    out = []
    p = src / "assigned.jsonl"
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            v = d.get("estimatedValue")
            if (d.get("zip") in zipkeep and d.get("neighborhood") not in nbcut
                    and not d.get("corporateOwned")
                    and str(d.get("id")) not in sold
                    and isinstance(v, (int, float)) and 1 <= v <= 700000):
                out.append(d)
    return out


def phase_master(args) -> None:
    """One workbook: every property, with its score where we have one.

    The market analysis and the distress scores were two files answering halves
    of the same question. This joins them, and states plainly which properties
    are scored, which have no imagery to score, and which have simply not been
    submitted yet, because those three are not the same thing and a blank cell
    cannot tell them apart.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    from lender_package import CALC_FILL, MONEY, TOTAL_FILL, _band, _polish

    universe = _cleaned_universe()
    gate(bool(universe), "no cleaned universe; run tn_market_analysis first")

    res = {}
    for d in _all_results():
        pid = str(d.get("PropertyID") or "")
        if pid:
            res[pid] = d
    log.info("universe %d | scored rows on disk %d", len(universe), len(res))

    scored, nocov, poor, unscored = [], [], [], []
    for r in universe:
        d = res.get(str(r["id"]))
        if d is None:
            unscored.append((r, None))
            continue
        sc = d.get("visual_distress_score")
        if sc is None:
            nocov.append((r, d))
        elif (d.get("photo_quality_score") or 0) < VALIDITY_FLOOR or \
                (d.get("house_visible_score") or 0) < VALIDITY_FLOOR:
            poor.append((r, d))
        else:
            scored.append((r, d))
    scored.sort(key=lambda t: -t[1]["visual_distress_score"])
    vals = sorted(d["visual_distress_score"] for _, d in scored)

    wb = Workbook()

    # ---- Overview --------------------------------------------------------
    ws = wb.active
    ws.title = "Overview"
    r = 1
    ws.cell(row=r, column=1,
            value="Knox and Blount: every property, with visual distress"
            ).font = Font(bold=True, size=14)
    r += 1
    ws.cell(row=r, column=1, value=datetime.now().strftime("%B %d, %Y"))
    r += 2
    _band(ws, r, "Where every property stands", span=3)
    r += 1
    n = len(universe)
    for label, val, note in (
        ("Properties surviving every exclusion", n,
         "ZIP, neighborhood, entity-held, sold since 2023, $1-700K buy box"),
        ("Scored and usable", len(scored), "the Call list sheet"),
        ("No Street View imagery", len(nocov), "unscoreable, not unblemished"),
        ("Image too poor to judge", len(poor), "excluded from ranking"),
        ("Not yet submitted", len(unscored),
         "$%s to finish" % format(round(len(unscored) * IMAGE_COST), ",")),
    ):
        ws.cell(row=r, column=1, value=label)
        c = ws.cell(row=r, column=2, value=val)
        c.number_format = "#,##0"
        c.fill = CALC_FILL
        if note:
            ws.cell(row=r, column=3, value=note).font = Font(size=9, italic=True)
        r += 1
    r += 1

    if vals:
        _band(ws, r, "Where a cut line lands", span=3)
        r += 1
        ws.cell(row=r, column=1, value="Score at or above").font = Font(bold=True)
        ws.cell(row=r, column=2, value="Properties").font = Font(bold=True)
        ws.cell(row=r, column=3, value="% of scored").font = Font(bold=True)
        r += 1
        for t in (90, 85, 75, 60, 50, 35):
            k = sum(1 for v in vals if v >= t)
            ws.cell(row=r, column=1, value=t)
            ws.cell(row=r, column=2, value=k).fill = CALC_FILL
            ws.cell(row=r, column=3, value=round(100.0 * k / len(vals), 1))
            r += 1
        r += 1
        ws.cell(row=r, column=1, value=(
            "35 and 75 are the vendor's national thresholds. This cohort runs a "
            "median of %d, so pick the row that gives a list a caller can "
            "finish rather than inheriting a number set elsewhere."
            % vals[len(vals) // 2]))
        ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True,
                                                       vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
        ws.row_dimensions[r].height = 44
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 54

    # ---- Call list (scored, ranked) --------------------------------------
    idx_heads = ["Rank", "Score", "VERIFY", "Address", "ZIP", "Neighborhood",
                 "County", "Est value", "Equity %", "Owner", "Year", "Beds",
                 "Baths", "Sqft", "Known distress",
                 "Abandoned", "Overall bad", "Exterior", "Roof", "Windows",
                 "Overgrown", "Cluttered yard", "Drives/walks", "Aged/outdated",
                 "Visible", "Photo", "Street View", "CRM record", "PropertyID"]
    ws2 = wb.create_sheet("Call list")
    for i, h in enumerate(idx_heads, 1):
        ws2.cell(row=1, column=i, value=h).font = Font(bold=True)
    amber = PatternFill("solid", fgColor="FFF2CC")
    rr = 2
    for rank, (m, d) in enumerate(scored, 1):
        hv = d.get("house_visible_score") or 0
        verify = "LOOK" if hv < 80 else ""
        owner = "Absentee" if m.get("absenteeOwner") else "Owner-occ"
        flags = ", ".join(x.replace("is_", "").replace("_", " ")
                          for x in (m.get("distress_active") or [])[:3])
        lat, lon = d.get("lat"), d.get("lon")
        sv = ("https://www.google.com/maps/@?api=1&map_action=pano"
              "&viewpoint=%s,%s" % (lat, lon)) if lat and lon else ""
        uuid = m.get("saved_uuid")
        row = [rank, d["visual_distress_score"], verify,
               d.get("address") or m.get("address"), m.get("zip"),
               d.get("neighborhood") or m.get("neighborhood"), m.get("county"),
               m.get("estimatedValue"), m.get("equityPercent"), owner,
               d.get("year_built") or m.get("year_built"), m.get("bedrooms"),
               m.get("bathrooms"), m.get("squareFeet"), flags,
               d.get("abandoned_score"), d.get("overall_bad_score"),
               d.get("bad_exterior_score"), d.get("bad_roof_score"),
               d.get("damaged_windows_score"), d.get("overgrown_score"),
               d.get("cluttered_yard_score"),
               d.get("bad_driveways_walkways_score"),
               d.get("aged_outdated_score"), hv, d.get("photo_quality_score"),
               sv, ("https://app.reisift.io/records/%s" % uuid) if uuid else "",
               d.get("PropertyID")]
        for i, v in enumerate(row, 1):
            c = ws2.cell(row=rr, column=i, value=v)
            if i == 8 and v:
                c.number_format = MONEY
            if verify:
                c.fill = amber
        for col, label in ((27, "view"), (28, "open")):
            cell = ws2.cell(row=rr, column=col)
            if cell.value:
                cell.hyperlink = cell.value
                cell.value = label
                cell.style = "Hyperlink"
        rr += 1
    ws2.freeze_panes = "D2"
    ws2.auto_filter.ref = "A1:AC%d" % max(1, rr - 1)
    for col, w in (("A", 6), ("B", 7), ("C", 8), ("D", 34), ("E", 8), ("F", 26),
                   ("G", 9), ("H", 12), ("I", 9), ("J", 11), ("K", 7), ("L", 6),
                   ("M", 7), ("N", 8), ("O", 24), ("P", 10), ("Q", 11), ("R", 9),
                   ("S", 7), ("T", 9), ("U", 10), ("V", 13), ("W", 12),
                   ("X", 13), ("Y", 8), ("Z", 7), ("AA", 8), ("AB", 11),
                   ("AC", 12)):
        ws2.column_dimensions[col].width = w

    # ---- All properties (the whole universe, lean) -----------------------
    ws3 = wb.create_sheet("All properties")
    heads3 = ["Status", "Score", "Address", "ZIP", "Neighborhood", "County",
              "Est value", "Equity %", "Owner", "Beds", "Baths", "Sqft",
              "Known distress", "PropertyID"]
    for i, h in enumerate(heads3, 1):
        ws3.cell(row=1, column=i, value=h).font = Font(bold=True)
    rr = 2
    buckets = ([("scored", m, d) for m, d in scored]
               + [("no imagery", m, d) for m, d in nocov]
               + [("image too poor", m, d) for m, d in poor]
               + [("not yet scored", m, None) for m, _ in unscored])
    for status, m, d in buckets:
        flags = ", ".join(x.replace("is_", "").replace("_", " ")
                          for x in (m.get("distress_active") or [])[:3])
        row = [status,
               (d or {}).get("visual_distress_score") if d else None,
               m.get("address"), m.get("zip"), m.get("neighborhood"),
               m.get("county"), m.get("estimatedValue"), m.get("equityPercent"),
               "Absentee" if m.get("absenteeOwner") else "Owner-occ",
               m.get("bedrooms"), m.get("bathrooms"), m.get("squareFeet"),
               flags, m.get("id")]
        for i, v in enumerate(row, 1):
            c = ws3.cell(row=rr, column=i, value=v)
            if i == 7 and v:
                c.number_format = MONEY
        rr += 1
    ws3.freeze_panes = "A2"
    ws3.auto_filter.ref = "A1:N%d" % max(1, rr - 1)
    for col, w in (("A", 15), ("B", 7), ("C", 34), ("D", 8), ("E", 26),
                   ("F", 9), ("G", 12), ("H", 9), ("I", 11), ("J", 6),
                   ("K", 7), ("L", 8), ("M", 24), ("N", 12)):
        ws3.column_dimensions[col].width = w

    # ---- By ZIP ----------------------------------------------------------
    ws4 = wb.create_sheet("By ZIP")
    agg = {}
    for status, m, d in buckets:
        z = agg.setdefault(m.get("zip") or "(blank)",
                           {"n": 0, "scored": 0, "s": [], "left": 0,
                            "county": m.get("county")})
        z["n"] += 1
        if status == "scored":
            z["scored"] += 1
            z["s"].append(d["visual_distress_score"])
        elif status == "not yet scored":
            z["left"] += 1
    for i, h in enumerate(["ZIP", "County", "Properties", "Scored",
                           "Not yet scored", "Cost to finish", "Median score",
                           "Score >=75", "Score >=90"], 1):
        ws4.cell(row=1, column=i, value=h).font = Font(bold=True)
    rr = 2
    for z, v in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        sv = sorted(v["s"])
        ws4.cell(row=rr, column=1, value=z)
        ws4.cell(row=rr, column=2, value=v["county"])
        ws4.cell(row=rr, column=3, value=v["n"])
        ws4.cell(row=rr, column=4, value=v["scored"])
        ws4.cell(row=rr, column=5, value=v["left"])
        c = ws4.cell(row=rr, column=6, value=round(v["left"] * IMAGE_COST, 2))
        c.number_format = MONEY
        ws4.cell(row=rr, column=7, value=sv[len(sv) // 2] if sv else None)
        ws4.cell(row=rr, column=8, value=sum(1 for x in sv if x >= 75))
        ws4.cell(row=rr, column=9, value=sum(1 for x in sv if x >= 90))
        rr += 1
    ws4.freeze_panes = "A2"
    for col, w in (("A", 9), ("B", 9), ("C", 12), ("D", 10), ("E", 15),
                   ("F", 14), ("G", 13), ("H", 11), ("I", 11)):
        ws4.column_dimensions[col].width = w

    # ---- Caveats ---------------------------------------------------------
    ws5 = wb.create_sheet("Sources and caveats")
    look = sum(1 for _, d in scored if (d.get("house_visible_score") or 0) < 80)
    notes = [
        ("What this file is",
         "Every Knox and Blount single-family property that survives the full "
         "exclusion stack, with its visual distress score where one exists. "
         "The market analysis and the scores used to be two files answering "
         "halves of the same question."),
        ("Exclusions already applied",
         "ZIP suppression, neighborhood suppression, entity-held owners, "
         "arms-length sales on or after 2023-01-01 at $1,000 or more, and the "
         "$1 to $700K buy box. Interfamily transfers and nominal deeds are "
         "deliberately KEPT: a quitclaim to an heir is an inherited property, "
         "not a market sale."),
        ("Three different blanks",
         "'no imagery' means Google has never photographed it, 'image too poor' "
         "means the model could not assess what it saw, and 'not yet scored' "
         "means we have not paid to look. None of them mean the house is fine."),
        ("VERIFY column",
         "%d scored rows (%.1f%%) have house_visible under 80. Their scores are "
         "unreliable in BOTH directions. The two condemned properties the "
         "calibration missed were deep-setback houses the camera could not see, "
         "scoring 29 and 18. Treat an amber row as unread."
         % (look, 100.0 * look / len(scored) if scored else 0)),
        ("Calibration",
         "7 of 9 known-condemned Knox properties ranked in the top 11 of 190 on "
         "a 250-property test, against a random-population median of 5."),
        ("No descriptions",
         "The vendor's LLM explanations are omitted. Their pipeline substitutes "
         "canned boilerplate when the model is rate-limited, and the "
         "no-distress variant was landing on properties it had scored 100. The "
         "SCORE is unaffected: it comes from the image model with no LLM."),
        ("Coverage",
         "Measured free across 1,500 Knox properties: 89.9% have Street View "
         "imagery, median vintage 2026, only 10.4% five or more years old."),
        ("Neighborhood assignment",
         "Neighborhood exists only on a rate-limited detail endpoint, so 25,069 "
         "properties were hydrated as anchors and the rest assigned by nearest "
         "neighbour on lat/lon. Holdout accuracy 95.2%, measured not assumed."),
    ]
    for i, (a, b) in enumerate(notes, 1):
        ws5.cell(row=i, column=1, value=a).font = Font(bold=True)
        c = ws5.cell(row=i, column=2, value=b)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws5.row_dimensions[i].height = 46
    ws5.column_dimensions["A"].width = 26
    ws5.column_dimensions["B"].width = 100

    try:
        _polish(ws)
    except Exception:
        pass
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["C"].width = 54

    final = (Path(args.out) if args.out else
             OUTPUT_ROOT / ("Knox_Blount_Master_%s.xlsx"
                            % datetime.now().strftime("%Y%m%d")))
    pending = final.with_name("_PENDING_" + final.name)
    wb.save(pending)
    try:
        if final.exists():
            final.unlink()
        pending.replace(final)
    except PermissionError:
        print()
        print("!! %s is OPEN IN EXCEL. Saved as %s instead."
              % (final.name, pending.name))
        final = pending

    print()
    print("universe %s | scored %s | no imagery %s | too poor %s | not yet %s"
          % (format(len(universe), ","), format(len(scored), ","),
             format(len(nocov), ","), format(len(poor), ","),
             format(len(unscored), ",")))
    if vals:
        print("at or above 90: %s   75: %s   50: %s"
              % (format(sum(1 for v in vals if v >= 90), ","),
                 format(sum(1 for v in vals if v >= 75), ","),
                 format(sum(1 for v in vals if v >= 50), ",")))
    print("written: %s" % final)


# ------------------------------------------------------------------ cli ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", required=True, choices=["doctor", "profile", "ids", "score", "collect",
                             "purge", "requeue", "report",
                             "master"])
    ap.add_argument("--address", default="1206 Connecticut Ave, Knoxville, TN 37921",
                    help="doctor: the address to resolve and score")
    ap.add_argument("--sample", type=int, default=1000,
                    help="profile: how many properties to check (free)")
    ap.add_argument("--county", default="", help="profile: Knox or Blount")
    ap.add_argument("--no-entity-filter", action="store_true",
                    help="ids: keep entity-held properties (default excludes)")
    ap.add_argument("--no-sold-filter", action="store_true",
                    help="ids: keep recently sold properties (default excludes)")
    ap.add_argument("--value-min", type=float, default=1)
    ap.add_argument("--value-max", type=float, default=700000)
    ap.add_argument("--zips", default="",
                    help="ids: comma-separated ZIPs to restrict the cohort to")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--all-zips", action="store_true",
                    help="ids: sample the whole universe, not just survivors")
    ap.add_argument("--batch", type=int, default=0,
                    help="submit at most this many fresh ids this run (bulk "
                         "caps at 10,000 per job); 0 means all of them")
    ap.add_argument("--cool-off", type=float, default=900,
                    help="seconds to wait since the previous job "
                         "before submitting another; 0 disables")
    ap.add_argument("--max-new", type=int, default=25,
                    help="refuse to submit more new ids than this")
    ap.add_argument("--commit", action="store_true",
                    help="actually spend money; dry run otherwise")
    ap.add_argument("--timeout", type=float, default=1800,
                    help="seconds to wait for a bulk job")
    ap.add_argument("--job-id", default="", help="collect: job to fetch")
    ap.add_argument("--out", default="", help="report: output xlsx path")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    OUT.mkdir(parents=True, exist_ok=True)
    {"doctor": phase_doctor, "profile": phase_profile,
     "ids": phase_ids, "score": phase_score,
     "collect": phase_collect, "purge": phase_purge,
     "requeue": phase_requeue,
     "report": phase_report, "master": phase_master}[a.phase](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
