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

    # Sample only from ZIPs that survived suppression, because those are the
    # only ones we would ever pay to score. Calibrating on a market we have
    # already ruled out would answer a question nobody asked.
    if getattr(args, "survivors_only", False):
        keep = _surviving_zips()
        if keep:
            before = len(rows)
            rows = [r for r in rows if r.get("zip") in keep]
            log.info("restricted to %d surviving ZIPs: %d -> %d rows",
                     len(keep), before, len(rows))
        else:
            log.warning("no suppression file found; sampling the whole universe")
    random.seed(args.seed)
    random.shuffle(rows)
    return rows[:args.sample]


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
              for r in rows if str(r.get("id")) not in ctrl_ids][:args.sample]

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
    log.info("job_id %s persisted", job)

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
    _collect(job, res, list(cohort.values()))


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


# ------------------------------------------------------------------ cli ----

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--phase", required=True, choices=["doctor", "profile", "ids", "score", "collect"])
    ap.add_argument("--address", default="1206 Connecticut Ave, Knoxville, TN 37921",
                    help="doctor: the address to resolve and score")
    ap.add_argument("--sample", type=int, default=1000,
                    help="profile: how many properties to check (free)")
    ap.add_argument("--county", default="", help="profile: Knox or Blount")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--all-zips", action="store_true",
                    help="ids: sample the whole universe, not just survivors")
    ap.add_argument("--max-new", type=int, default=25,
                    help="refuse to submit more new ids than this")
    ap.add_argument("--commit", action="store_true",
                    help="actually spend money; dry run otherwise")
    ap.add_argument("--timeout", type=float, default=1800,
                    help="seconds to wait for a bulk job")
    ap.add_argument("--job-id", default="", help="collect: job to fetch")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    OUT.mkdir(parents=True, exist_ok=True)
    {"doctor": phase_doctor, "profile": phase_profile,
     "ids": phase_ids, "score": phase_score,
     "collect": phase_collect}[a.phase](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
