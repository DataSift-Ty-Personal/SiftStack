"""Rebuild ty+2's Not Interested campaigns + retire Tier 2 (Doors per Deal, 2026-08-25).

Ty's decisions (recorded in the plan): the account runs Priority 1 / Priority 2 / FTM
only. Tier 2 is fully retired (presets, folders, tag off all 18K records, tag deleted).
The BULK folders become the coherent untagged-remainder lane (the future "Priority 3").
The legacy `default` folder's 22 pre-Doors-per-Deal presets are deleted with backup.
NI cadence: monthly for P1/P2 (45d is not an expressible rolling boundary; the filter
UI's "prior to" anchor only resolves today/yesterday/week/month/quarter/year, proven
from the app bundle), quarterly for the Priority 3 remainder.

Phases: backup reactivation tier2 bulk legacy hygiene qa. DRY by default.

  python src/ty2_reactivation_rebuild.py --dry            # everything, no writes
  python src/ty2_reactivation_rebuild.py --commit         # everything
  python src/ty2_reactivation_rebuild.py --phase qa       # assertion pass only
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import urllib.error
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from obituary_opportunity import Reader  # noqa: E402

ACCOUNT = "datasift-apikey"          # ty+2 no-expiry Api-Key
ACCOUNT_UUID = "bfa7e948-fab0-4819-8635-b15c3f8bcfd4"
BACKUP_DIR = Path(__file__).resolve().parent.parent / "output" / "backups" / f"ty2_reactivation_{date.today():%Y%m%d}"

REACT_FOLDER = "12. REACTIVATION"
PROTO_PRESET = "Not Interested - FTM rest (90d)"   # its must_not is the suppression block
TIER2_FOLDERS = ("07. TIER 2 - DISTRESSORS - CALL", "08. TIER 2 - DISTRESSORS - MAIL")
BULK_FOLDERS = ("09. BULK - CALL", "10. BULK - MAIL")
FTM_RENAMES = {"05. TIER 1 - FTM - CALL": "05. FTM - CALL",
               "06. TIER 1 - FTM - MAIL": "06. FTM - MAIL"}
NI_RENAMES = {"Not Interested - FTM Foreclosure (15d)": "Not Interested - FTM Foreclosure (30d)",
              "Not Interested - FTM Probate (45d)": "Not Interested - FTM Probate (30d)"}
DELETE_NI = "Not Interested - Tier 2 (45d)"
REHASH = "Rehash - Ready (never answered)"
JUNK_TAG = "Courthouse Data, code_violation, Knox"


def aslist(r):
    if isinstance(r, dict):
        return r.get("results") or r.get("data") or []
    return r or []


class Ctx:
    def __init__(self, commit: bool):
        self.rd = Reader(ACCOUNT, gap=0.5)
        self.commit = commit
        # transient-network retry around every call (OneDrive wifi resets kill long runs)
        inner = self.rd._call
        def call(path, **kw):
            for attempt in range(4):
                try:
                    return inner(path, **kw)
                except (OSError, urllib.error.URLError) as e:
                    if attempt == 3:
                        raise
                    print(f"    network blip on {path} ({str(e)[:60]}); retry in {5 * (attempt + 1)}s")
                    time.sleep(5 * (attempt + 1))
        self.rd._call = call
        self.refresh()

    def refresh(self):
        rd = self.rd
        self.tags = {t["title"]: t["uuid"] for t in aslist(rd._call("/api/internal/tag/?limit=10000&ordering=title"))}
        self.lists = {l["title"]: l["uuid"] for l in aslist(rd._call("/api/internal/list/?limit=999"))}
        self.folders = {f["title"]: f for f in aslist(rd._call("/api/internal/filter-preset-folder/?type=properties&limit=999"))}
        self.presets = {}   # folder title -> [preset dicts]
        self.by_title = {}  # preset title -> preset (titles are globally unique)
        for ft, f in self.folders.items():
            rows = aslist(rd._call(f"/api/internal/filter-preset-folder/{f['uuid']}/filter-preset/?limit=999"))
            self.presets[ft] = rows
            for p in rows:
                self.by_title[p["title"]] = p

    def count(self, must):
        r = self.rd._call("/api/internal/property/", method="POST", override="GET",
                          body={"limit": 1, "offset": 0, "query": {"must": must}})
        return r.get("count", 0)

    def write(self, label, fn):
        """Gate every mutation on --commit; always narrate."""
        if self.commit:
            out = fn()
            print(f"  [W] {label}")
            return out
        print(f"  [dry] {label}")
        return None


# ── phases ─────────────────────────────────────────────────────────────

def phase_backup(c: Ctx):
    print("\n== backup ==")
    snap = {
        "pulled": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tags": c.tags, "lists": c.lists,
        "folders": {t: f for t, f in c.folders.items()},
        "presets": c.presets,
        "sequences": aslist(c.rd._call("/api/internal/sequence/?limit=999")),
        "statuses": aslist(c.rd._call("/api/internal/status/?limit=1000")),
    }
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKUP_DIR / "snapshot.json"
    out.write_text(json.dumps(snap, indent=1, default=str), encoding="utf-8")
    n = sum(len(v) for v in c.presets.values())
    print(f"  saved {len(c.folders)} folders / {n} presets / {len(c.tags)} tags -> {out}")


def _suppression_must_not(c: Ctx):
    proto = c.by_title.get(PROTO_PRESET) or c.by_title.get(PROTO_PRESET.replace("(90d)", "(90d) "))
    if not proto:
        raise SystemExit(f"prototype preset {PROTO_PRESET!r} not found; cannot clone suppression block")
    mn = copy.deepcopy(((proto.get("filters") or {}).get("must") or {}).get("must_not") or {})
    if not mn:
        raise SystemExit(f"{PROTO_PRESET!r} has an empty must_not; refusing to build unsuppressed presets")
    return mn


def _mk_preset(c: Ctx, title, folder_uuid, must):
    body = {"title": title, "folder": folder_uuid, "quick_filter": False,
            "type": "properties", "filters": {"must": must, "account": ACCOUNT_UUID}}
    def go():
        r = c.rd._call("/api/internal/filter-preset/", method="POST", body=body)
        u = r.get("uuid")
        back = c.rd._call(f"/api/internal/filter-preset/{u}/")
        got = (back.get("filters") or {}).get("must") or {}
        if got != must:
            raise SystemExit(f"read-back mismatch on {title!r}:\n want {json.dumps(must)[:400]}\n got  {json.dumps(got)[:400]}")
        return u
    c.write(f"create preset {title!r}", go)


def _patch_preset(c: Ctx, preset, *, filters=None, title=None, label=""):
    body = {}
    if filters is not None:
        body["filters"] = filters
    if title is not None:
        body["title"] = title
    def go():
        c.rd._call(f"/api/internal/filter-preset/{preset['uuid']}/", method="PATCH", body=body)
        back = c.rd._call(f"/api/internal/filter-preset/{preset['uuid']}/")
        if filters is not None and (back.get("filters") or {}).get("must") != filters["must"]:
            raise SystemExit(f"PATCH read-back mismatch on {preset['title']!r}")
        if title is not None and back.get("title") != title:
            raise SystemExit(f"rename did not stick on {preset['title']!r}")
    c.write(label or f"patch preset {preset['title']!r}", go)


def phase_reactivation(c: Ctx):
    print("\n== reactivation (folder 12 rebuild) ==")
    folder = c.folders.get(REACT_FOLDER)
    if not folder:
        raise SystemExit(f"folder {REACT_FOLDER!r} not found")
    mn = _suppression_must_not(c)
    P1, P2, FTM = c.tags["Priority 1"], c.tags["Priority 2"], c.tags["FTM"]

    def win(tok):
        return [{"field": "status", "options": ["36-months", tok]}]

    plans = [
        ("Not Interested - Priority 1 (30d)",
         {"any_tags": [P1], "any_property_status": ["not_interested"],
          "last_updated_date": win("month"), "must_not": copy.deepcopy(mn)}),
        ("Not Interested - Priority 2 (30d)", None),   # filled below (must_not += P1)
        ("Not Interested - Priority 3 (90d)", None),
    ]
    mn_p2 = copy.deepcopy(mn)
    mn_p2["any_tags"] = sorted(set(mn_p2.get("any_tags") or []) | {P1})
    plans[1] = ("Not Interested - Priority 2 (30d)",
                {"any_tags": [P2], "any_property_status": ["not_interested"],
                 "last_updated_date": win("month"), "must_not": mn_p2})
    mn_p3 = copy.deepcopy(mn)
    mn_p3["any_tags"] = sorted(set(mn_p3.get("any_tags") or []) | {P1, P2, FTM})
    plans[2] = ("Not Interested - Priority 3 (90d)",
                {"any_property_status": ["not_interested"],
                 "last_updated_date": win("quarter"), "must_not": mn_p3})

    for title, must in plans:
        if title in c.by_title:
            print(f"  exists, skipping: {title!r}")
            continue
        probe = {k: v for k, v in must.items() if k != "last_updated_date"}
        print(f"  {title!r} would match {c.count(probe)} records (date gate not counted; UI-resolved)")
        _mk_preset(c, title, folder["uuid"], must)

    # Rehash: re-anchor Tier 2 -> P1/P2
    rehash = c.by_title.get(REHASH)
    if rehash:
        filt = copy.deepcopy(rehash["filters"])
        cur = filt["must"].get("any_tags") or []
        want = sorted({P1, P2})
        if sorted(cur) != want:
            filt["must"]["any_tags"] = want
            _patch_preset(c, rehash, filters=filt, label=f"re-anchor {REHASH!r} to Priority 1/2")
        else:
            print(f"  {REHASH!r} already anchored on P1/P2")
    else:
        print(f"  WARNING: {REHASH!r} not found")

    for old, new in NI_RENAMES.items():
        p = c.by_title.get(old)
        if p:
            _patch_preset(c, p, title=new, label=f"rename {old!r} -> {new!r}")
        elif new not in c.by_title:
            print(f"  WARNING: neither {old!r} nor {new!r} found")

    doomed = c.by_title.get(DELETE_NI)
    if doomed:
        c.write(f"delete preset {DELETE_NI!r}",
                lambda: c.rd._call(f"/api/internal/filter-preset/{doomed['uuid']}/", method="DELETE"))
    else:
        print(f"  already gone: {DELETE_NI!r}")


def phase_tier2(c: Ctx):
    print("\n== tier2 (full retire) ==")
    # 1. folders 07/08: presets then folders
    for ft in TIER2_FOLDERS:
        f = c.folders.get(ft)
        if not f:
            print(f"  already gone: folder {ft!r}")
            continue
        for p in c.presets.get(ft, []):
            c.write(f"delete preset {p['title']!r}",
                    lambda u=p["uuid"]: c.rd._call(f"/api/internal/filter-preset/{u}/", method="DELETE"))
        c.write(f"delete folder {ft!r}",
                lambda u=f["uuid"]: c.rd._call(f"/api/internal/filter-preset-folder/{u}/", method="DELETE"))

    # 2. strip the Tier 2 tag account-wide (target = every record carrying it)
    t2 = c.tags.get("Tier 2")
    if t2:
        n = c.count({"any_tags": [t2]})
        print(f"  Tier 2 tag currently on {n} records")
        if n:
            payload = {"query": {"must": {"any_tags": [t2]}}, "tags": ["Tier 2"]}
            assert payload["tags"] == ["Tier 2"]   # belt and suspenders: this strips account-wide
            c.write(f"remove-tags 'Tier 2' from all {n} records",
                    lambda: c.rd._call("/api/internal/property/remove-tags/", method="POST", body=payload))
            if c.commit:
                for i in range(20):
                    time.sleep(15)
                    left = c.count({"any_tags": [t2]})
                    print(f"    index catching up: {left} still tagged")
                    if left == 0:
                        break
    # 3. delete the tags (index lags ~30s after a mass remove; retry)
    for name in ("Tier 2", "Tier 1"):
        u = c.tags.get(name)
        if not u:
            print(f"  tag already gone: {name!r}")
            continue
        def kill(u=u, name=name):
            for attempt in range(6):
                try:
                    c.rd._call(f"/api/internal/tag/{u}/", method="DELETE")
                    return
                except RuntimeError as e:
                    print(f"    delete {name!r} attempt {attempt + 1} failed ({str(e)[:80]}); waiting 30s")
                    time.sleep(30)
            raise SystemExit(f"could not delete tag {name!r} after 6 attempts")
        c.write(f"delete tag {name!r}", kill)

    # 4. de-tier the FTM folder names
    for old, new in FTM_RENAMES.items():
        f = c.folders.get(old)
        if not f:
            print(f"  folder rename already done or missing: {old!r}")
            continue
        def rn(f=f, new=new):
            c.rd._call(f"/api/internal/filter-preset-folder/{f['uuid']}/", method="PATCH", body={"title": new})
            back = c.rd._call(f"/api/internal/filter-preset-folder/{f['uuid']}/")
            if back.get("title") != new:
                raise SystemExit(f"folder rename did not stick: {old!r}")
        c.write(f"rename folder {old!r} -> {new!r}", rn)


def phase_bulk(c: Ctx):
    print("\n== bulk (coherent untagged-remainder / future Priority 3) ==")
    P1, P2, FTM = c.tags["Priority 1"], c.tags["Priority 2"], c.tags["FTM"]
    t2 = c.tags.get("Tier 2")   # may already be deleted; still scrub the uuid from refs
    t2_uuids = {t2} if t2 else set()
    # scrub any historical tier-2 uuid even after tag deletion
    t2_uuids.add("c35ab897-540d-4f35-9f67-ca5b062cbee5")
    anchors = {P1, P2}

    for ft in BULK_FOLDERS:
        for p in c.presets.get(ft, []):
            filt = copy.deepcopy(p["filters"])
            must = filt.get("must") or {}
            changed = False
            cur_anchor = must.get("any_tags") or []
            if cur_anchor:   # bulk means NO tier tags; drop the inverted anchor
                must.pop("any_tags", None)
                changed = True
            mn = must.setdefault("must_not", {})
            cur = [u for u in (mn.get("any_tags") or []) if u not in t2_uuids]
            want = sorted(set(cur) | anchors | {FTM})
            if want != sorted(mn.get("any_tags") or []):
                mn["any_tags"] = want
                changed = True
            if changed:
                _patch_preset(c, p, filters=filt, label=f"fix bulk polarity {p['title']!r}")
            else:
                print(f"  ok: {p['title']!r}")


def phase_legacy(c: Ctx):
    print("\n== legacy (default folder cleanup) ==")
    rows = c.presets.get("default", [])
    print(f"  {len(rows)} legacy presets (backed up in the backup phase)")
    for p in rows:
        c.write(f"delete legacy preset {p['title']!r}",
                lambda u=p["uuid"]: c.rd._call(f"/api/internal/filter-preset/{u}/", method="DELETE"))


def phase_hygiene(c: Ctx):
    print("\n== hygiene ==")
    # dedupe doubled Low Equity / Negative Equity refs in the FTM folders' presets
    ftm_folders = [t for t in c.folders if t in FTM_RENAMES or t in FTM_RENAMES.values()]
    for ft in ftm_folders:
        for p in c.presets.get(ft, []):
            filt = copy.deepcopy(p["filters"])
            mn = (filt.get("must") or {}).get("must_not") or {}
            deduped = False
            for key in ("any_lists", "any_tags"):
                v = mn.get(key)
                if v and len(v) != len(dict.fromkeys(v)):
                    mn[key] = list(dict.fromkeys(v))
                    deduped = True
            if deduped:
                _patch_preset(c, p, filters=filt, label=f"dedupe must_not refs in {p['title']!r}")

    junk = c.tags.get(JUNK_TAG)
    if junk:
        n = c.count({"any_tags": [junk]})
        print(f"  junk tag {JUNK_TAG!r} on {n} records")
        if n:
            payload = {"query": {"must": {"any_tags": [junk]}}, "tags": [JUNK_TAG]}
            c.write(f"remove junk tag from {n} records",
                    lambda: c.rd._call("/api/internal/property/remove-tags/", method="POST", body=payload))
            if c.commit:
                time.sleep(30)
        def kill():
            for attempt in range(6):
                try:
                    c.rd._call(f"/api/internal/tag/{junk}/", method="DELETE")
                    return
                except RuntimeError as e:
                    print(f"    junk tag delete attempt {attempt + 1} failed ({str(e)[:80]}); waiting 30s")
                    time.sleep(30)
            print("  WARNING: junk tag delete did not stick; rerun hygiene later")
        c.write(f"delete tag {JUNK_TAG!r}", kill)
    else:
        print("  junk tag already gone")


def phase_qa(c: Ctx):
    print("\n== qa ==")
    c.refresh()
    ok = True

    def check(label, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
        ok = ok and cond

    react = {p["title"] for p in c.presets.get(REACT_FOLDER, [])}
    for t in ("Not Interested - Priority 1 (30d)", "Not Interested - Priority 2 (30d)",
              "Not Interested - Priority 3 (90d)", "Not Interested - FTM Foreclosure (30d)",
              "Not Interested - FTM Probate (30d)", "Not Interested - FTM rest (90d)", REHASH):
        check(f"folder 12 has {t!r}", t in react)
    check(f"folder 12 dropped {DELETE_NI!r}", DELETE_NI not in react)
    check("folder 12 has exactly 7 presets", len(react) == 7)

    for ft in TIER2_FOLDERS:
        check(f"folder gone: {ft!r}", ft not in c.folders)
    for name in ("Tier 2", "Tier 1"):
        check(f"tag gone: {name!r}", name not in c.tags)
    for old, new in FTM_RENAMES.items():
        check(f"folder renamed to {new!r}", new in c.folders and old not in c.folders)
    check("legacy default folder empty", len(c.presets.get("default", [])) == 0)

    # no preset anywhere references the deleted tag uuids
    dead = {"c35ab897-540d-4f35-9f67-ca5b062cbee5", "d3fe5a1b-5b42-4d60-a1bc-9de6682ba8df"}
    dangling = [p["title"] for rows in c.presets.values() for p in rows
                if dead & set(json.dumps(p.get("filters") or {}).split('"'))]
    check("no dangling Tier 1/2 uuid in any preset", not dangling)
    if dangling:
        print("    dangling in:", dangling)

    # bulk polarity
    P1, P2, FTM = c.tags.get("Priority 1"), c.tags.get("Priority 2"), c.tags.get("FTM")
    for ft in BULK_FOLDERS:
        for p in c.presets.get(ft, []):
            must = (p.get("filters") or {}).get("must") or {}
            mn_tags = set((must.get("must_not") or {}).get("any_tags") or [])
            check(f"bulk coherent: {p['title']!r}",
                  not must.get("any_tags") and {P1, P2, FTM} <= mn_tags)

    # tag counts
    for name in ("Priority 1", "Priority 2", "FTM"):
        u = c.tags.get(name)
        print(f"  count {name}: {c.count({'any_tags': [u]}) if u else 'MISSING'}")
    print(f"  count not_interested total: {c.count({'any_property_status': ['not_interested']})}")

    print("\n  QA:", "ALL PASS" if ok else "FAILURES ABOVE")
    return ok


PHASES = {"backup": phase_backup, "reactivation": phase_reactivation, "tier2": phase_tier2,
          "bulk": phase_bulk, "legacy": phase_legacy, "hygiene": phase_hygiene, "qa": phase_qa}
ORDER = ["backup", "reactivation", "tier2", "bulk", "legacy", "hygiene", "qa"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--phase", choices=ORDER + ["all"], default="all")
    args = ap.parse_args()
    commit = args.commit and not args.dry

    c = Ctx(commit)
    print(f"mode: {'COMMIT' if commit else 'DRY RUN'} | account: {ACCOUNT} ({ACCOUNT_UUID})")
    phases = ORDER if args.phase == "all" else [args.phase]
    for ph in phases:
        PHASES[ph](c)
        if ph != "qa" and commit:
            c.refresh()   # dry runs mutate nothing; skip the 20-call refresh
    if not commit and args.phase != "qa":
        print("\nDRY RUN complete, nothing written. Rerun with --commit.")


if __name__ == "__main__":
    main()
