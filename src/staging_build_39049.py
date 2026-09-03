"""One-shot build-out of the ty+1 staging account for Franklin County OH.

Phases (every write phase is DRY by default; add --commit):

    python src/staging_build_39049.py --phase preflight
    python src/staging_build_39049.py --phase sequences --commit
    python src/staging_build_39049.py --phase size
    python src/staging_build_39049.py --phase pull --commit
    python src/staging_build_39049.py --phase feeders --commit
    python src/staging_build_39049.py --phase crm --commit
    python src/staging_build_39049.py --phase qa

Source of truth is the County List Playbook shard
https://learn.datasift.ai/county-data/39.json key "39049". P1 rows are pulled
into lists, P2 rows become auto-add feeder presets only (Ty's call,
2026-08-19). AI rows are excluded (ty+1 has no AI addons) and the judicial
notice keys are excluded as churn artifacts (Ohio is judicial; the playbook
itself demotes them).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reisift_session import Session, api_call, load_staff_jwt  # noqa: E402
import siftmap_pull  # noqa: E402
from siftmap_pull import Map, usage  # noqa: E402

FIPS = "39049"
COUNTY_LABEL = "Franklin OH"
SHARD_URL = "https://learn.datasift.ai/county-data/39.json"
MAP = "https://map.reisift.io"

OUT = os.environ.get("SIFTSTACK_OUTPUT_DIR", "output")
STATE_FILE = os.path.join(OUT, "staging_39049_state.json")
SHARD_CACHE = os.path.join(OUT, "county-data-39.json")
BACKUP_DIR = os.path.join(OUT, "backups")
QA_FILE = os.path.join(OUT, "staging_39049_qa.json")

# Judicial-regime churn artifacts: near-empty non-judicial notice feeds that
# look like 141x lifts on 20-record lists. The playbook's own verify rule
# forces them P3; belt and suspenders here.
EXCLUDE_KEYS = {"is_notice_of_default", "is_notice_of_foreclosure"}

NICHE_FOLDER = "00 Niche Sequential Marketing"


# ---------------------------------------------------------------- data layer

def fetch_shard() -> dict:
    if os.path.exists(SHARD_CACHE):
        with open(SHARD_CACHE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        req = urllib.request.Request(SHARD_URL, headers={"accept": "*/*"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        os.makedirs(OUT, exist_ok=True)
        with open(SHARD_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    county = data.get(FIPS)
    if not county:
        raise SystemExit("shard has no key %s" % FIPS)
    return county


def ascii_clean(s: str) -> str:
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
        else:
            out.append("-")
    return " ".join("".join(out).split())


def select_segments(county: dict) -> list[dict]:
    """P1 + P2 rows that are actually pullable through SiftMap filters."""
    segs = []
    for row in county.get("rows", []):
        pr = row.get("priority")
        if pr not in (1, 2):
            continue
        if row.get("type") == "ai":
            continue  # ty+1 has no AI addons; AI is a parallel pull anyway
        keys = row.get("keys") or []
        if not keys:
            continue  # e.g. Obituary: a data list, not a SiftMap filter
        if set(keys) & EXCLUDE_KEYS:
            continue
        segs.append(dict(row))
    segs.sort(key=lambda r: (r["priority"], -(r.get("lift") or 0)))
    ranks = {1: 0, 2: 0}
    for s in segs:
        ranks[s["priority"]] += 1
        s["rank"] = "P%d-%02d" % (s["priority"], ranks[s["priority"]])
        seg_name = ascii_clean(s.get("seg") or "+".join(s["keys"]))
        s["list_title"] = "%s %s - %s" % (COUNTY_LABEL, s["rank"], seg_name)
        s["feeder_name"] = s["list_title"] + " - Feeder"
        s["tags"] = ["P%d" % s["priority"], "%s %s" % (COUNTY_LABEL, FIPS),
                     "SiftMap"]
        s["description"] = ascii_clean(
            "Priority %d %s (%s). %s. %s deals, %s doors/deal, %sx lift, "
            "conf %s. Buy box $1-700k SFR off-market. Playbook plan: %s." % (
                s["priority"], COUNTY_LABEL, FIPS, seg_name,
                s.get("deals"), s.get("dpd"), s.get("lift"),
                s.get("conf"), s.get("plan")))
    return segs


# ---------------------------------------------------------------- state file

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"segments": {}, "sequences": {}, "crm": {}}


def save_state(state: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ------------------------------------------------------------- CRM helpers

def crm_list_uuids(s: Session) -> dict:
    r = s.call("/api/internal/list/?limit=999")
    return {x["title"]: x["uuid"] for x in r.get("results", [])}


def crm_tag_uuids(s: Session) -> dict:
    r = s.call("/api/internal/tag/?offset=0&limit=10000&ordering=title")
    return {x["title"]: x["uuid"] for x in r.get("results", [])}


def crm_count(s: Session, must: dict) -> int:
    r = s.call("/api/internal/property/", method="POST",
               method_override="GET",
               body={"limit": 1, "offset": 0, "query": {"must": must}})
    return int(r.get("count", 0))


def map_get(s: Session, path: str):
    return api_call(s.token, path, base=MAP)


def all_feeders(s: Session) -> list[dict]:
    out, page = [], 1
    while True:
        r = map_get(s, "/filters/?scope=account&page_size=100&page=%d" % page)
        results = r.get("results", r if isinstance(r, list) else [])
        out.extend(results)
        if not r.get("next") or not results:
            return out
        page += 1


# ------------------------------------------------------------------- phases

def phase_preflight(s: Session, state: dict, args) -> None:
    ident = s.verify_target()
    print("target account: %s (account %s, impersonated=%s, token exp %s)"
          % (ident["email"], ident["account"], ident["impersonated"],
             ident["exp_iso"]))
    print("feature flags: %s" % ", ".join(ident["feature_flags"]))
    print("addons: %s" % ", ".join(ident["addons"]) or "(none)")
    used, limit = usage(s)
    print("record allowance: %s used of %s (%s left)"
          % (f"{used:,}", f"{limit:,}", f"{limit - used:,}"))
    county = fetch_shard()
    segs = select_segments(county)
    n1 = sum(1 for x in segs if x["priority"] == 1)
    print("\nsegment plan: %d P1 (pull) + %d P2 (feeder only)"
          % (n1, len(segs) - n1))
    for x in segs:
        print("  %s  dpd %-6s lift %-5s deals %-4s est.size %-6s %s"
              % (x["rank"], x.get("dpd"), x.get("lift"), x.get("deals"),
                 x.get("list_size"), x["list_title"]))
    state["ident"] = ident
    state["allowance"] = {"used": used, "limit": limit, "at": time.time()}
    save_state(state)


def phase_sequences(s: Session, state: dict, args) -> None:
    s.verify_target()
    listing = s.call("/api/internal/sequence/?limit=999")
    rows = listing.get("results", [])
    print("sequences in %s: %d" % (s.target_email, len(rows)))
    full = []
    for row in rows:
        uid = row.get("uuid") or row.get("id")
        try:
            detail = s.call("/api/internal/sequence/%s/" % uid)
        except Exception as e:  # keep the thin row if detail read fails
            detail = dict(row, _detail_error=str(e))
        full.append(detail)
        time.sleep(0.3)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = os.path.join(BACKUP_DIR, "sequences_ty1_%s.json" % ts)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(full, f, indent=1)
    print("backup written: %s" % backup)
    for row in rows:
        print("  %s  active=%s  %s" % (row.get("uuid") or row.get("id"),
                                       row.get("is_active"),
                                       row.get("title")))
    if not args.commit:
        print("DRY RUN: no deletions. Re-run with --commit to delete ALL of "
              "the above.")
        return
    deleted = []
    for row in rows:
        uid = row.get("uuid") or row.get("id")
        s.call("/api/internal/sequence/%s/" % uid, method="DELETE")
        deleted.append(uid)
        time.sleep(0.4)
    left = s.call("/api/internal/sequence/?limit=999").get("results", [])
    print("deleted %d; remaining after read-back: %d" % (len(deleted),
                                                         len(left)))
    state["sequences"] = {"backup": backup, "deleted": deleted,
                          "remaining": len(left), "at": time.time()}
    save_state(state)
    if left:
        for row in left:
            print("  STILL PRESENT: %s %s" % (row.get("uuid"),
                                              row.get("title")))
        raise SystemExit("sequence deletion incomplete")


def phase_size(s: Session, state: dict, args) -> None:
    county = fetch_shard()
    segs = select_segments(county)
    m = Map(s)
    used, limit = usage(s)
    total_p1 = 0
    print("%-7s %-9s %-8s %s" % ("rank", "size", "status", "list"))
    for x in segs:
        rec = state["segments"].setdefault(x["rank"], {})
        rec.update({k: x[k] for k in ("rank", "priority", "list_title",
                                      "feeder_name", "tags", "description")})
        rec["keys"] = x["keys"]
        try:
            n = m.size(FIPS, x["keys"])
            status = "OK" if n > 0 else "GATED-OR-EMPTY"
        except Exception as e:
            n, status = 0, "GATED-OR-EMPTY"
            rec["size_error"] = str(e)[:220]
        rec["size"] = n
        rec["sized_at"] = time.time()
        rec["status"] = status
        if x["priority"] == 1 and status == "OK":
            total_p1 += n
        print("%-7s %-9s %-8s %s" % (x["rank"], f"{n:,}", status,
                                     x["list_title"]))
        time.sleep(0.5)
    save_state(state)
    print("\nP1 total (pullable): %s   allowance left: %s"
          % (f"{total_p1:,}", f"{limit - used:,}"))
    if total_p1 > limit - used:
        print("NO-GO: P1 total exceeds remaining allowance.")
    else:
        print("GO: P1 fits the remaining allowance.")


def phase_pull(s: Session, state: dict, args) -> None:
    ident = s.verify_target()
    segs = [r for r in state["segments"].values()
            if r["priority"] == 1 and r.get("status") == "OK"]
    segs.sort(key=lambda r: r["rank"])
    if args.only:
        segs = [r for r in segs if r["rank"] == args.only or args.only == "P1"]
    stale = [r for r in segs if time.time() - r.get("sized_at", 0) > 86400]
    if stale:
        raise SystemExit("sizes are stale (>24h) for %s - re-run --phase size"
                         % ", ".join(r["rank"] for r in stale))
    used, limit = usage(s)
    need = sum(r["size"] for r in segs if not r.get("pulled"))
    print("pulling %d segments, %s records max, %s allowance left"
          % (sum(1 for r in segs if not r.get("pulled")),
             f"{need:,}", f"{limit - used:,}"))
    if need > limit - used:
        raise SystemExit("HARD STOP: pull total %s > allowance left %s"
                         % (f"{need:,}", f"{limit - used:,}"))
    m = Map(s)
    for r in segs:
        if r.get("pulled"):
            print("  %s already pulled, skipping" % r["rank"])
            continue
        print("  %s -> list %r (%s records)" % (r["rank"], r["list_title"],
                                                f"{r['size']:,}"))
        if not args.commit:
            continue
        resp = m.add(FIPS, r["keys"], lists=[r["list_title"]], tags=r["tags"])
        r["pulled"] = True
        r["pulled_at"] = time.time()
        r["add_resp"] = resp if isinstance(resp, (dict, str)) else str(resp)
        save_state(state)
        u2, l2 = usage(s)
        print("    added; allowance now %s used of %s" % (f"{u2:,}",
                                                          f"{l2:,}"))
        time.sleep(1.0)
    if not args.commit:
        print("DRY RUN: nothing pulled. Re-run with --commit.")


def phase_feeders(s: Session, state: dict, args) -> None:
    s.verify_target()
    existing = {f.get("name"): f for f in all_feeders(s)}
    m = Map(s)
    segs = [r for r in state["segments"].values() if r.get("status") == "OK"]
    segs.sort(key=lambda r: r["rank"])
    for r in segs:
        if r["feeder_name"] in existing:
            print("  %s feeder exists, skipping" % r["rank"])
            r["feeder_id"] = existing[r["feeder_name"]].get("id") \
                or existing[r["feeder_name"]].get("uuid")
            continue
        print("  %s -> feeder %r" % (r["rank"], r["feeder_name"]))
        if not args.commit:
            continue
        resp = m.save_preset(r["feeder_name"], FIPS, r["keys"],
                             lists=[r["list_title"]], tags=r["tags"],
                             description=r["description"])
        r["feeder_id"] = (resp or {}).get("id") or (resp or {}).get("uuid")
        save_state(state)
        time.sleep(0.6)
    if not args.commit:
        print("DRY RUN: no feeders saved. Re-run with --commit.")
        return
    names = {f.get("name") for f in all_feeders(s)}
    missing = [r["rank"] for r in segs if r["feeder_name"] not in names]
    if missing:
        raise SystemExit("feeders missing after read-back: %s"
                         % ", ".join(missing))
    print("feeder read-back OK: %d present" % len(segs))


# The Day 2 challenge guide's 12-preset niche sequential system
# (learn.datasift.ai/niche-sequential-marketing; concrete filter blocks from
# dist/sequential-presets.skill references/filter-configurations.md), adapted
# to the Franklin P1 lists. Cadence tags are created up front. must grammar
# per _api/reference/datasift-api.md section A7. The one block the grammar
# cannot express (09's "Last Updated Field: Status prior to 3 months ago")
# is skipped and logged.
CADENCE_TAGS = ["sms_sent", "called_day1", "called_day2", "called_day3",
                "mailed", "cycle_complete", "dp_complete",
                "callback_scheduled", "hot", "not_interested", "bad_data"]
# Priority + provenance tags. The SiftMap pulls apply P1 / Franklin OH 39049 /
# SiftMap by NAME, so pre-creating them binds the pulls to these exact tag
# objects; Courthouse Data is the challenge's niche anchor for future FTM
# uploads (any_tags is OR, so either population matches).
ANCHOR_TAGS = ["P1", "P2", "%s %s" % (COUNTY_LABEL, FIPS), "SiftMap",
               "Courthouse Data"]


def niche_presets(p1_lists: list[str], tag: dict) -> list[dict]:
    NOT_SOLD = {"any_property_status": ["sold"]}
    NOT_SOLD_NI = {"any_property_status": ["sold", "not_interested"]}
    anchor = [tag["P1"], tag["Courthouse Data"]]

    def t(name):
        return [tag[name]]

    return [
        {"title": "00. Needs Skip Traced",
         "must": {"any_lists": p1_lists, "any_tags": anchor,
                  "predictivecall_attempts": [0, 0],
                  "phone": 0, "skiptraced": 0, "must_not": dict(NOT_SOLD)}},
        {"title": "01. Ready to Text",
         "must": {"any_lists": p1_lists, "any_tags": anchor,
                  "phone": 1, "skiptraced": 1,
                  "must_not": dict(NOT_SOLD, any_tags=t("sms_sent"))}},
        {"title": "02. Needs Called Day 1",
         "must": {"any_lists": p1_lists, "any_tags": t("sms_sent"),
                  "phone": 1,
                  "must_not": dict(NOT_SOLD_NI, any_tags=t("called_day1"))}},
        {"title": "03. Needs Called Day 2",
         "must": {"any_lists": p1_lists, "any_tags": t("called_day1"),
                  "phone": 1,
                  "must_not": dict(NOT_SOLD_NI, any_tags=t("called_day2"))}},
        {"title": "04. Needs Called Day 3",
         "must": {"any_lists": p1_lists, "any_tags": t("called_day2"),
                  "phone": 1,
                  "must_not": dict(NOT_SOLD_NI, any_tags=t("called_day3"))}},
        {"title": "05. Needs Mailed",
         "must": {"any_tags": t("called_day3"),
                  "directmail_attempts": [0, 0], "owner_vacant": 0,
                  "must_not": dict(NOT_SOLD, any_tags=t("mailed"))}},
        {"title": "06. Needs Deep Prospecting",
         "must": {"any_tags": t("cycle_complete"),
                  "must_not": dict(NOT_SOLD, any_tags=t("dp_complete"))}},
        {"title": "07. Callback Scheduled",
         "must": {"any_tags": t("callback_scheduled"),
                  "must_not": dict(NOT_SOLD)}},
        {"title": "08. Hot Lead",
         "must": {"any_tags": t("hot"), "must_not": dict(NOT_SOLD)}},
        # Spec also wants "Status last updated prior to 3 months ago";
        # not expressible in the documented must grammar - noted in QA.
        {"title": "09. Not Interested",
         "must": {"any_tags": t("not_interested"), "phone": 1,
                  "any_property_status": ["not_interested"]}},
        {"title": "10. Bad Data",
         "must": {"any_tags": t("bad_data"),
                  "any_phone_status": ["WRONG", "WRONG_DNC", "DEAD"],
                  "must_not": dict(NOT_SOLD)}},
        {"title": "11. Completed Cycle",
         "must": {"any_tags": t("cycle_complete"),
                  "must_not": dict(NOT_SOLD, any_tags=t("hot"))}},
    ]


def phase_crm(s: Session, state: dict, args) -> None:
    ident = s.verify_target()
    dst_lists = crm_list_uuids(s)
    p1 = [r for r in state["segments"].values()
          if r["priority"] == 1 and r.get("status") == "OK"]
    # Pre-create any P1 list that the async SiftMap adds have not
    # materialized yet: the add attaches by NAME, so records land in these
    # exact list objects when the queue drains.
    p1_uuids = []
    for r in sorted(p1, key=lambda x: x["rank"]):
        u = dst_lists.get(r["list_title"])
        if not u:
            print("  pre-creating list %r" % r["list_title"])
            if args.commit:
                u = s.call("/api/internal/list/", method="POST",
                           body={"title": r["list_title"]}).get("uuid")
                time.sleep(0.3)
            else:
                u = "DRY-RUN-UUID"
        p1_uuids.append(u)
    if not p1_uuids:
        raise SystemExit("no P1 segments in state - run --phase size first")

    dst_tags = crm_tag_uuids(s)
    for title in CADENCE_TAGS + ANCHOR_TAGS:
        if title in dst_tags:
            continue
        print("  creating tag %r" % title)
        if args.commit:
            r = s.call("/api/internal/tag/", method="POST",
                       body={"title": title})
            dst_tags[title] = r.get("uuid")
            time.sleep(0.3)
        else:
            dst_tags[title] = "DRY-RUN-UUID"

    folders = s.call("/api/internal/filter-preset-folder/?type=properties"
                     "&limit=999").get("results", [])
    folder = next((f for f in folders
                   if f.get("title", "").upper() == NICHE_FOLDER.upper()),
                  None)
    if not folder:
        print("creating folder %r" % NICHE_FOLDER)
        if args.commit:
            folder = s.call("/api/internal/filter-preset-folder/",
                            method="POST",
                            body={"title": NICHE_FOLDER,
                                  "type": "properties", "permissions": []})
    have = set()
    if folder:
        have = {p.get("title") for p in
                s.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
                       % folder["uuid"]).get("results", [])}

    plan = niche_presets(p1_uuids, dst_tags)
    made = []
    for p in plan:
        if p["title"] in have:
            print("  %r exists, skipping" % p["title"])
            continue
        print("  creating preset %r" % p["title"])
        if not args.commit:
            continue
        s.call("/api/internal/filter-preset/", method="POST",
               body={"title": p["title"], "folder": folder["uuid"],
                     "quick_filter": False,
                     "filters": {"must": p["must"],
                                 "account": ident["account"]},
                     "type": "properties"})
        made.append(p["title"])
        time.sleep(0.5)
    if not args.commit:
        print("DRY RUN: nothing created. Re-run with --commit.")
        return
    back = {p.get("title") for p in
            s.call("/api/internal/filter-preset-folder/%s/filter-preset/?limit=999"
                   % folder["uuid"]).get("results", [])}
    missing = [p["title"] for p in plan if p["title"] not in back]
    state["crm"] = {"folder": folder["uuid"], "created": made,
                    "missing": missing, "at": time.time()}
    save_state(state)
    if missing:
        raise SystemExit("presets missing after read-back: %s" % missing)
    print("CRM read-back OK: %d presets in %r" % (len(back), NICHE_FOLDER))


def phase_qa(s: Session, state: dict, args) -> None:
    ident = s.verify_target()
    qa = {"ident": ident, "at": time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                              time.gmtime()),
          "sequences": {}, "lists": [], "feeders": [], "crm": {}}
    # 1. sequences empty
    seqs = s.call("/api/internal/sequence/?limit=999").get("results", [])
    qa["sequences"] = {"remaining": len(seqs),
                       "backup": state.get("sequences", {}).get("backup"),
                       "pass": len(seqs) == 0}
    # 2. list counts vs sizing
    lists = crm_list_uuids(s)
    for r in sorted(state["segments"].values(), key=lambda x: x["rank"]):
        if not r.get("pulled"):
            continue
        uuid = lists.get(r["list_title"])
        n = crm_count(s, {"any_lists": [uuid]}) if uuid else 0
        size = r.get("size") or 0
        drift = abs(n - size) / size if size else 1.0
        status = ("PASS" if drift <= 0.05 else
                  "WARN" if drift <= 0.15 else "FAIL")
        if n == 0:
            status = "FAIL"
        qa["lists"].append({"rank": r["rank"], "list": r["list_title"],
                            "sized": size, "count": n, "status": status})
        print("%-7s sized %-8s count %-8s %s" % (r["rank"], f"{size:,}",
                                                 f"{n:,}", status))
        time.sleep(0.4)
    # 3. feeders
    feeders = {f.get("name"): f for f in all_feeders(s)}
    for r in sorted(state["segments"].values(), key=lambda x: x["rank"]):
        if r.get("status") != "OK":
            continue
        f = feeders.get(r["feeder_name"])
        ok = bool(f) and f.get("auto_add_enabled") is True
        qa["feeders"].append({"rank": r["rank"], "feeder": r["feeder_name"],
                              "present": bool(f), "auto_add": ok})
        if not ok:
            print("FEEDER ISSUE %s: present=%s auto_add=%s"
                  % (r["rank"], bool(f), f.get("auto_add_enabled")
                     if f else None))
    # 4. usage
    used, limit = usage(s)
    qa["allowance"] = {"used": used, "limit": limit}
    # 5. crm mirror: 20 folders, per-folder counts vs the ty+2 export
    state.pop("crm", None)  # superseded by the 20-folder mirror
    if os.path.exists(MIRROR_CACHE):
        with open(MIRROR_CACHE, encoding="utf-8") as f:
            src = json.load(f)
        dst = {x.get("title"): x for x in
               s.call("/api/internal/filter-preset-folder/?type=properties"
                      "&limit=999").get("results", [])}
        rows, ok = [], True
        for fol in src["folders"]:
            d = dst.get(fol["title"])
            n = len(s.call("/api/internal/filter-preset-folder/%s/"
                           "filter-preset/?limit=999"
                           % d["uuid"]).get("results", [])) if d else 0
            good = n == len(fol["presets"])
            ok = ok and good
            rows.append({"folder": fol["title"], "presets": n,
                         "expected": len(fol["presets"]), "pass": good})
            time.sleep(0.3)
        qa["crm"] = {"folders": rows, "pass": ok}
        print("crm mirror: %d folders, all counts %s"
              % (len(rows), "MATCH" if ok else "MISMATCH"))
    gated = [r["rank"] for r in state["segments"].values()
             if r.get("status") == "GATED-OR-EMPTY"]
    qa["gated_segments"] = gated
    os.makedirs(OUT, exist_ok=True)
    with open(QA_FILE, "w", encoding="utf-8") as f:
        json.dump(qa, f, indent=1)
    print("\nsequences remaining: %d | allowance %s/%s | gated: %s"
          % (len(seqs), f"{used:,}", f"{limit:,}", ", ".join(gated) or "none"))
    print("QA written: %s" % QA_FILE)


# ------------------------------------------- 20-folder challenge mirror

MIRROR_CACHE = os.path.join(OUT, "ty2_preset_mirror.json")
UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _probe_map(call, paths, key="title"):
    """First endpoint that answers wins; returns {uuid: title}."""
    for p in paths:
        try:
            r = call(p)
            rows = r.get("results", r if isinstance(r, list) else [])
            if isinstance(rows, list):
                return {x["uuid"]: x.get(key) for x in rows
                        if isinstance(x, dict) and x.get("uuid")}
        except Exception:
            continue
    return {}


BOARD_PATHS = ["/api/internal/board/?limit=999",
               "/api/internal/boards/?limit=999",
               "/api/internal/siftline/board/?limit=999"]
USER_PATHS = ["/api/internal/account/user/?limit=999",
              "/api/internal/user/?limit=999",
              "/api/internal/users/?limit=999"]


def phase_crm_clear(s: Session, state: dict, args) -> None:
    s.verify_target()
    folders = s.call("/api/internal/filter-preset-folder/?type=properties"
                     "&limit=999").get("results", [])
    folder = next((f for f in folders
                   if f.get("title", "").upper() == NICHE_FOLDER.upper()),
                  None)
    if not folder:
        print("folder %r not present in ty+1 - nothing to clear" %
              NICHE_FOLDER)
        return
    rows = s.call("/api/internal/filter-preset-folder/%s/filter-preset/"
                  "?limit=999" % folder["uuid"]).get("results", [])
    print("clearing folder %r: %d presets" % (NICHE_FOLDER, len(rows)))
    for p in rows:
        print("  delete preset %r" % p.get("title"))
    if not args.commit:
        print("DRY RUN: nothing deleted. Re-run with --commit.")
        return
    for p in rows:
        s.call("/api/internal/filter-preset/%s/" % p["uuid"],
               method="DELETE")
        time.sleep(0.4)
    s.call("/api/internal/filter-preset-folder/%s/" % folder["uuid"],
           method="DELETE")
    left = [f for f in s.call("/api/internal/filter-preset-folder/"
                              "?type=properties&limit=999").get("results", [])
            if f.get("title", "").upper() == NICHE_FOLDER.upper()]
    if left:
        raise SystemExit("folder still present after delete")
    print("cleared: folder and %d presets gone (read-back OK)" % len(rows))


def _ty2_export() -> dict:
    if os.path.exists(MIRROR_CACHE):
        with open(MIRROR_CACHE, encoding="utf-8") as f:
            return json.load(f)
    staff_jwt = load_staff_jwt(min_left_s=600)

    def staff(path, **kw):
        return api_call(staff_jwt, path, **kw)

    folders = [f for f in staff("/api/internal/filter-preset-folder/"
                                "?type=properties&limit=999").get("results",
                                                                  [])
               if f.get("title", "")[:3].rstrip(".").isdigit()]
    folders.sort(key=lambda f: f["title"])
    out = {"folders": []}
    for f in folders:
        rows = staff("/api/internal/filter-preset-folder/%s/filter-preset/"
                     "?limit=999" % f["uuid"]).get("results", [])
        presets = []
        for p in rows:
            d = staff("/api/internal/filter-preset/%s/" % p["uuid"])
            presets.append({"title": d.get("title"),
                            "quick_filter": bool(d.get("quick_filter")),
                            "filters": d.get("filters") or {}})
            time.sleep(0.25)
        out["folders"].append({"title": f["title"], "presets": presets})
        print("  exported %-40s %d presets" % (f["title"], len(presets)))
    out["lists"] = {x["uuid"]: x["title"] for x in
                    staff("/api/internal/list/?limit=999").get("results", [])}
    out["tags"] = {x["uuid"]: x["title"] for x in
                   staff("/api/internal/tag/?offset=0&limit=10000"
                         "&ordering=title").get("results", [])}
    out["boards"] = _probe_map(staff, BOARD_PATHS)
    out["users"] = _probe_map(staff, USER_PATHS, key="first_name")
    try:
        out["statuses"] = [x.get("title") for x in
                           staff("/api/internal/status/?limit=1000"
                                 ).get("results", [])]
    except Exception as e:
        out["statuses"] = []
        print("  ty+2 status read failed: %s" % str(e)[:120])
    os.makedirs(OUT, exist_ok=True)
    with open(MIRROR_CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    return out


class _Xlate:
    """uuid -> title -> ty+1 uuid, creating lists/tags on demand."""

    def __init__(self, s, src, ident, commit):
        self.s, self.src, self.commit = s, src, commit
        self.account = ident["account"]
        from reisift_session import decode_claims
        self.self_user = decode_claims(s.token).get("user_id")
        self.lists = crm_list_uuids(s)
        self.tags = crm_tag_uuids(s)
        self.boards = {v: k for k, v in
                       _probe_map(s.call, BOARD_PATHS).items() if v}
        self.users = {v: k for k, v in
                      _probe_map(s.call, USER_PATHS, key="first_name"
                                 ).items() if v}
        self.log = []

    def _ensure(self, kind, title):
        pool = self.lists if kind == "list" else self.tags
        if title in pool:
            return pool[title]
        if not self.commit:
            pool[title] = "DRY-RUN-UUID"
            return pool[title]
        r = self.s.call("/api/internal/%s/" % kind, method="POST",
                        body={"title": title})
        pool[title] = r.get("uuid")
        self.log.append("created %s %r" % (kind, title))
        time.sleep(0.3)
        return pool[title]

    def walk(self, node, preset):
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ("any_lists", "all_lists"):
                    vals = []
                    for u in (v if isinstance(v, list) else [v]):
                        title = self.src["lists"].get(u)
                        if title and UUID_RE.match(title):
                            self.log.append(
                                "%s: junk uuid-titled list -> Auction"
                                % preset)
                            title = "Auction"
                        if title:
                            vals.append(self._ensure("list", title))
                        else:
                            self.log.append("%s: DROPPED unknown list %s"
                                            % (preset, u))
                    if vals:
                        out[k] = vals
                elif k in ("any_tags", "all_tags"):
                    vals = []
                    for u in (v if isinstance(v, list) else [v]):
                        title = self.src["tags"].get(u)
                        if title:
                            vals.append(self._ensure("tag", title))
                        else:
                            self.log.append("%s: DROPPED unknown tag %s"
                                            % (preset, u))
                    if vals:
                        out[k] = vals
                elif k == "any_boards":
                    vals = []
                    for u in (v if isinstance(v, list) else [v]):
                        title = self.src["boards"].get(u)
                        tgt = self.boards.get(title) if title else None
                        if tgt:
                            vals.append(tgt)
                        else:
                            self.log.append(
                                "%s: DROPPED board ref %r (no ty+1 board)"
                                % (preset, title or u))
                    if vals:
                        out[k] = vals
                elif k == "assigned_to":
                    name = self.src["users"].get(v)
                    tgt = self.users.get(name) if name else None
                    if not tgt:
                        tgt = self.self_user
                        self.log.append(
                            "%s: assigned_to %r -> ty+1 self (placeholder)"
                            % (preset, name or v))
                    out[k] = tgt
                elif k == "account":
                    out[k] = self.account
                else:
                    out[k] = self.walk(v, preset)
            return out
        if isinstance(node, list):
            return [self.walk(v, preset) for v in node]
        return node


def phase_crm_mirror(s: Session, state: dict, args) -> None:
    ident = s.verify_target()
    src = _ty2_export()
    x = _Xlate(s, src, ident, args.commit)

    # status parity: create ty+2 custom statuses missing from ty+1
    try:
        mine = [t.get("title") for t in
                s.call("/api/internal/status/?limit=1000").get("results", [])]
        for t in src.get("statuses", []):
            if t and t not in mine:
                print("  status missing in ty+1: %r" % t)
                if args.commit:
                    try:
                        s.call("/api/internal/status/", method="POST",
                               body={"title": t})
                        x.log.append("created status %r" % t)
                        time.sleep(0.3)
                    except Exception as e:
                        x.log.append("status %r NOT creatable: %s"
                                     % (t, str(e)[:100]))
    except Exception as e:
        print("  status parity skipped: %s" % str(e)[:120])

    dst_folders = {f.get("title"): f for f in
                   s.call("/api/internal/filter-preset-folder/"
                          "?type=properties&limit=999").get("results", [])}
    made = 0
    for fol in src["folders"]:
        dst = dst_folders.get(fol["title"])
        if not dst:
            print("folder %r" % fol["title"])
            if args.commit:
                dst = s.call("/api/internal/filter-preset-folder/",
                             method="POST",
                             body={"title": fol["title"],
                                   "type": "properties", "permissions": []})
                time.sleep(0.3)
        have = set()
        if dst:
            have = {p.get("title") for p in s.call(
                "/api/internal/filter-preset-folder/%s/filter-preset/"
                "?limit=999" % dst["uuid"]).get("results", [])}
        for p in fol["presets"]:
            if p["title"] in have:
                print("    %r exists, skipping" % p["title"])
                continue
            filters = x.walk(p["filters"], p["title"])
            print("    preset %r" % p["title"])
            if not args.commit:
                continue
            s.call("/api/internal/filter-preset/", method="POST",
                   body={"title": p["title"], "folder": dst["uuid"],
                         "quick_filter": p["quick_filter"],
                         "filters": filters, "type": "properties"})
            made += 1
            time.sleep(0.4)
    print("\ntranslation log (%d):" % len(x.log))
    for line in x.log:
        print("  " + line)
    if not args.commit:
        print("DRY RUN: nothing created. Re-run with --commit.")
        return
    # read-back: per-folder counts must match the export
    bad = []
    fresh = {f.get("title"): f for f in
             s.call("/api/internal/filter-preset-folder/"
                    "?type=properties&limit=999").get("results", [])}
    for fol in src["folders"]:
        dst = fresh.get(fol["title"])
        n = len(s.call("/api/internal/filter-preset-folder/%s/filter-preset/"
                       "?limit=999" % dst["uuid"]).get("results", [])) \
            if dst else 0
        mark = "OK" if n == len(fol["presets"]) else "MISMATCH"
        if mark != "OK":
            bad.append(fol["title"])
        print("  %-42s %d/%d %s" % (fol["title"], n, len(fol["presets"]),
                                    mark))
    state["crm_mirror"] = {"created": made, "log": x.log, "bad": bad,
                           "at": time.time()}
    save_state(state)
    if bad:
        raise SystemExit("folders with count mismatch: %s" % bad)
    print("mirror read-back OK: %d folders" % len(src["folders"]))


PHASES = {"preflight": phase_preflight, "sequences": phase_sequences,
          "size": phase_size, "pull": phase_pull, "feeders": phase_feeders,
          "crm": phase_crm, "crm-clear": phase_crm_clear,
          "crm-mirror": phase_crm_mirror, "qa": phase_qa}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--only", help="restrict pull to one rank, e.g. P1-03")
    args = ap.parse_args()
    s = Session()
    state = load_state()
    PHASES[args.phase](s, state, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
