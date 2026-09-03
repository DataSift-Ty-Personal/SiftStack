"""Anchor the dispo blast cohort to a list and a sequential preset flow.

The blast needs somewhere to live in the CRM, for two reasons. A filter preset
is how `seed.from_preset` finds who to text, and the preset's `sms_attempts`
counter is what stops anyone being texted twice: a record leaves the Ready lane
the moment `crm.bump_sms_attempts` increments it after a successful send. No
bookkeeping tags of our own, which is the same grammar Priority 1 uses.

    python src/dispo_flow.py --phase list       # who is in the cohort
    python src/dispo_flow.py --phase list --commit
    python src/dispo_flow.py --phase presets --commit
    python src/dispo_flow.py --phase verify

CONTRACTS, each of which fails quietly if you get it wrong:
  * `POST /api/internal/property/{uuid}/add-lists/` takes `lists` as a STRING.
    An array returns 201 and does nothing, which is how a batch reports success
    while changing nothing (the same trap `strip_bulk_lists` documents).
  * Folder preset listing defaults to 10 rows, so pass `?limit=999` or the
    exists-check lies and re-creating 400s on the global unique-title rule.
  * The cohort EXCLUDES institutional buyers, iBuyers and SFR funds per Ty
    (registry tiers 2 and 3, plus EXCLUDE). Opendoor and D.R. Horton are not
    dispo customers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import config  # noqa: F401
    OUTPUT_ROOT = Path(getattr(config, "OUTPUT_DIR", "output"))
except Exception:
    OUTPUT_ROOT = Path("output")

log = logging.getLogger("dispo_flow")

OUT = OUTPUT_ROOT / "dispo_buyers"
LIST_TITLE = "Dispo - Deal Blast"
FOLDER_TITLE = "21. Dispo Sequential Marketing"

EXCLUDE_TYPES = ("institutional", "not a target")
EXCLUDE_TIERS = ("2", "3", "EXCLUDE")


def _load(name):
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def cohort() -> list:
    """The buyers who actually get texted."""
    profiles = _load("buyer_profiles.json") or []
    reg = {b["buyer_key"]: b for b in (_load("registry.json") or [])}
    out = []
    for p in profiles:
        b = reg.get(p["buyer_key"]) or {}
        if not p.get("reachable") or not b.get("saved_uuid"):
            continue
        if p.get("buyer_type") in EXCLUDE_TYPES or b.get("tier") in EXCLUDE_TIERS:
            continue
        out.append((p, b))
    return out


def _lists(c) -> dict:
    res = c._request("/api/internal/list/?limit=999")
    return {(r.get("title") or "").strip(): r.get("uuid")
            for r in (res.get("results") or [])}


def _tags(c) -> dict:
    res = c._request("/api/internal/tag/?limit=999")
    return {(r.get("title") or "").strip(): r.get("uuid")
            for r in (res.get("results") or [])}


def presets_for(list_uuid: str, sold_tag: str = "") -> list:
    """Five lanes, one list. Deliberately smaller than the acquisitions set.

    Progression rides the DIALER's own counters, not tags we maintain, which is
    the Priority 1 grammar. Two departures from it, both on purpose:

    THE SOLD EXCLUSION IS GONE. Every acquisitions preset carries must_not
    status "sold" because on the seller side a sold property is a dead lead.
    Here the record is on the list PRECISELY because a sale happened: the buyer
    is whoever bought it. Measured live, inheriting that exclusion cut 199
    qualifying buyers to 26. `not_interested` still applies, that being a buyer
    who told us no.

    THE SKIP-TRACE TRIAGE LANES ARE GONE. "Needs Skipped" and "Skipped No
    Numbers" are empty by construction now the phonebook is written, and an
    always-empty lane is clutter that makes the folder harder to read.
    """
    L = [list_uuid]
    NOT_INTERESTED = {"any_property_status": ["not_interested"]}
    return [
        {"title": "Dispo - 01 Ready to Text",
         "must": {"any_lists": L, "phone": 1, "sms_attempts": [0, 0],
                  "must_not": dict(NOT_INTERESTED)}},
        {"title": "Dispo - 02 Texted",
         "must": {"any_lists": L, "phone": 1, "sms_attempts": [1, 1],
                  "must_not": dict(NOT_INTERESTED)}},
        {"title": "Dispo - 03 Ready to Call",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [0, 0],
                  "must_not": dict(NOT_INTERESTED)}},
        {"title": "Dispo - 04 Call Attempt 1",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [1, 1],
                  "must_not": dict(NOT_INTERESTED)}},
        {"title": "Dispo - 05 Call Attempt 2",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [2, 2],
                  "must_not": dict(NOT_INTERESTED)}},
    ]


def phase_clean(args) -> None:
    """Delete everything in the folder, then rebuild the five lanes.

    Rebuilding beats patching: the folder had accumulated two overlapping sets
    of seven, one of them carrying the wrong sold exclusion, and reconciling
    that in place is harder to verify than starting from a known shape.
    """
    from sms_agent import crm
    c = crm.client()
    lists = _lists(c)
    list_uuid = lists.get(LIST_TITLE)
    if not list_uuid:
        raise SystemExit("run --phase list first; %r does not exist" % LIST_TITLE)

    folders = c._request("/api/internal/filter-preset-folder/"
                         "?type=properties&limit=999").get("results") or []
    folder = next((f for f in folders
                   if (f.get("title") or "").upper() == FOLDER_TITLE.upper()), None)
    if not folder:
        raise SystemExit("folder %r not found" % FOLDER_TITLE)

    # limit=999 or the listing returns 10 and the delete misses the rest
    existing = c._request("/api/internal/filter-preset-folder/%s/filter-preset/"
                          "?limit=999" % folder["uuid"]).get("results") or []
    wanted = presets_for(list_uuid)
    print("folder %r currently holds %d presets" % (FOLDER_TITLE, len(existing)))
    for e in existing:
        print("   delete  %s" % (e.get("title") or "")[:46])
    print("then create %d:" % len(wanted))
    for w in wanted:
        print("   create  %s" % w["title"])
    if not args.commit:
        print("")
        print("DRY RUN. Re-run with --commit.")
        return

    gone = 0
    for e in existing:
        try:
            c._request("/api/internal/filter-preset/%s/" % e.get("uuid"),
                       method="DELETE")
            gone += 1
        except Exception as ex:  # noqa: BLE001
            log.warning("delete failed for %s: %s",
                        (e.get("title") or "")[:34], str(ex)[:90])
        time.sleep(0.3)
    print("deleted %d of %d" % (gone, len(existing)))

    made = 0
    for w in wanted:
        c._request("/api/internal/filter-preset/", method="POST",
                   body={"title": w["title"], "folder": folder["uuid"],
                         "quick_filter": False,
                         "filters": {"must": w["must"]},
                         "type": "properties"})
        made += 1
        time.sleep(0.3)

    back = c._request("/api/internal/filter-preset-folder/%s/filter-preset/"
                      "?limit=999" % folder["uuid"]).get("results") or []
    titles = {x.get("title") for x in back}
    missing = [w["title"] for w in wanted if w["title"] not in titles]
    extra = [t for t in titles if t not in {w["title"] for w in wanted}]
    if missing or extra:
        raise SystemExit("read-back FAILED. missing=%s extra=%s" % (missing, extra))
    for x in back:
        mn = ((x.get("filters") or {}).get("must") or {}).get("must_not") or {}
        if "sold" in (mn.get("any_property_status") or []):
            raise SystemExit("read-back FAILED: %r still excludes sold" % x.get("title"))
    print("read-back OK: exactly %d presets, none excluding sold" % len(back))


def phase_list(args) -> None:
    from sms_agent import crm
    c = crm.client()
    people = cohort()
    print("blast cohort: %d buyers" % len(people))
    named = sum(1 for p, _ in people if p.get("name_known"))
    print("  named %d | entity with a number but no person %d"
          % (named, len(people) - named))

    lists = _lists(c)
    uuid = lists.get(LIST_TITLE)
    if not uuid:
        print("list %r does not exist" % LIST_TITLE)
        if not args.commit:
            print("DRY RUN. Re-run with --commit.")
            return
        uuid = c._request("/api/internal/list/", method="POST",
                          body={"title": LIST_TITLE}).get("uuid")
        print("  created %s" % uuid)
    else:
        print("list %r exists (%s)" % (LIST_TITLE, uuid))

    if not args.commit:
        print("would add %d records to it" % len(people))
        print("DRY RUN. Re-run with --commit.")
        return

    added = failed = 0
    for i, (p, b) in enumerate(people, 1):
        try:
            # STRING, not an array. An array 201s and does nothing.
            c._request("/api/internal/property/%s/add-lists/" % b["saved_uuid"],
                       method="POST", body={"lists": LIST_TITLE})
            added += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.warning("add-lists failed for %s: %s", b["name"][:30], str(e)[:100])
        time.sleep(0.25)
        if i % 50 == 0:
            log.info("added %s/%s", i, len(people))
    print("list: %d added, %d failed" % (added, failed))


def phase_presets(args) -> None:
    from sms_agent import crm
    c = crm.client()
    lists = _lists(c)
    list_uuid = lists.get(LIST_TITLE)
    if not list_uuid:
        raise SystemExit("run --phase list first; %r does not exist" % LIST_TITLE)
    sold = _tags(c).get("recently sold", "")
    if not sold:
        print("WARN no 'recently sold' tag found; the self-cleaning must_not "
              "will be weaker than the existing dispo presets")

    folders = c._request("/api/internal/filter-preset-folder/"
                         "?type=properties&limit=999").get("results") or []
    folder = next((f for f in folders
                   if (f.get("title") or "").upper() == FOLDER_TITLE.upper()), None)
    if not folder:
        print("folder %r missing" % FOLDER_TITLE)
        if not args.commit:
            return
        folder = c._request("/api/internal/filter-preset-folder/", method="POST",
                            body={"title": FOLDER_TITLE, "type": "properties",
                                  "permissions": []})
    # limit=999 or the exists-check lies and re-creation 400s on unique title
    have = {p.get("title") for p in
            (c._request("/api/internal/filter-preset-folder/%s/filter-preset/"
                        "?limit=999" % folder["uuid"]).get("results") or [])}

    wanted = presets_for(list_uuid, sold)
    by_title = {x.get("title"): x for x in
                (c._request("/api/internal/filter-preset-folder/%s/filter-preset/"
                            "?limit=999" % folder["uuid"]).get("results") or [])}
    for p in wanted:
        cur = by_title.get(p["title"])
        if cur:
            live = ((cur.get("filters") or {}).get("must")) or {}
            if live == p["must"]:
                print("  ok      %s" % p["title"])
                continue
            print("  UPDATE  %s" % p["title"])
            if not args.commit:
                continue
            # Read-modify-write the whole filters block; a partial PATCH is
            # accepted and silently keeps the old must.
            body = dict(cur)
            body["filters"] = dict(cur.get("filters") or {})
            body["filters"]["must"] = p["must"]
            c._request("/api/internal/filter-preset/%s/" % cur.get("uuid"),
                       method="PATCH", body=body)
            time.sleep(0.3)
            continue
        print("  create  %s" % p["title"])
        if not args.commit:
            continue
        c._request("/api/internal/filter-preset/", method="POST",
                   body={"title": p["title"], "folder": folder["uuid"],
                         "quick_filter": False,
                         "filters": {"must": p["must"]},
                         "type": "properties"})
        time.sleep(0.3)

    if args.commit:
        back = {p.get("title") for p in
                (c._request("/api/internal/filter-preset-folder/%s/filter-preset/"
                            "?limit=999" % folder["uuid"]).get("results") or [])}
        missing = [p["title"] for p in wanted if p["title"] not in back]
        if missing:
            raise SystemExit("presets missing after read-back: %s" % missing)
        print("read-back OK: all %d presets present in %r" % (len(wanted), FOLDER_TITLE))
    else:
        print("DRY RUN. Re-run with --commit.")


def phase_verify(args) -> None:
    """The only check that matters: can the seeder actually see the cohort."""
    from sms_agent import seed
    people = cohort()
    rows, matched = seed.from_preset("Dispo Blast - 02 Ready to Text")
    print("cohort                        : %d" % len(people))
    print("preset %-22r: %d mobile rows with a qualifying dial tier"
          % (matched or "NOT FOUND", len(rows)))
    if rows:
        for r in rows[:5]:
            print("   %-12s %-28s %s" % (r["phone"], (r.get("owner") or "")[:28],
                                         (r.get("street") or "")[:26]))
    if not matched:
        raise SystemExit("preset not found; run --phase presets --commit")
    if not rows:
        raise SystemExit("preset matched but returned no textable rows; check "
                         "that the phonebook writeback ran")


PHASES = {"list": phase_list, "clean": phase_clean,
          "presets": phase_presets, "verify": phase_verify}


def main() -> int:
    ap = argparse.ArgumentParser(description="Dispo blast list and preset flow")
    ap.add_argument("--phase", required=True, choices=sorted(PHASES))
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    PHASES[a.phase](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
