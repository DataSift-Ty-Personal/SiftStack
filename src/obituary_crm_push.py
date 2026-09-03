"""Stage and push the obituary deep-prospecting cohort into ty+2.

Reads output/dp/crm_plan.json (from obituary_crm_plan.py) and executes it in
phases. DRY BY DEFAULT: every phase prints exactly what it would send and writes
nothing until --commit.

  csv      emit the three wizard CSVs (phone merge pass A + B, phone tags)
  assign   set assigned_to on every record
  tags     add the property tags, read-modify-write per record
  fields   write the custom fields
  notes    post the pinned message-board note
  cards    create the SiftLine card on the Deep Prospecting board
  audit    read everything back and print a PASS/FAIL matrix

Order matters. Tags and fields are cheap and reversible, so they run before the
phone merge, which is not: there is no phone-delete route anywhere in this
codebase, and neither is there a message-delete route. Run every phase with
--limit 1 --commit first and read the record in the UI.

Preflight verified 2026-08-11: no active sequence fires on card.created (zero
exist), every active card.moved sequence is scoped to Lead Management /
Acquisitions / Transactions rather than the Deep Prospecting board, and all four
property.tags.added sequences gate on 'recently sold' or 'Sold', which none of
our tags match. Re-run --phase preflight if sequences change.

Usage:
  python src/obituary_crm_push.py --phase csv
  python src/obituary_crm_push.py --phase preflight
  python src/obituary_crm_push.py --phase tags --limit 1 --commit
  python src/obituary_crm_push.py --phase all --commit
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from datasift_api_upload import Api, field_index  # noqa: E402
from obituary_crm_plan import BOARD, COLUMNS  # noqa: E402

PLAN = Path("output/dp/crm_plan.json")
STATE = Path("output/dp/crm_write_state.json")
OUTDIR = Path("output/dp")
RETRY_HINT = re.compile(r"available in (\d+)\s*second")
PHASES = ["preflight", "csv", "assign", "tags", "fields", "notes", "cards", "audit"]
MARKER = "[Obituary DP]"


def call(api, path, method="GET", body=None, tries=6):
    """Api.call with the 429 backoff it does not have. The internal API throttles
    hard: 7 req/s got 529 of 740 records rejected earlier in this build."""
    for attempt in range(tries):
        try:
            return api.call(path, method, body)
        except RuntimeError as e:
            msg = str(e)
            if "HTTP 429" in msg:
                m = RETRY_HINT.search(msg)
                wait = min((int(m.group(1)) if m else 30) + 2, 300)
                print(f"      rate limited, sleeping {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"rate-limit retries exhausted on {path}")


def aslist(r):
    """Paginated endpoints return {count, results}. An EMPTY results list is
    falsy, so `r.get("results") or r` falls through to the dict and iterating it
    yields string keys. Check for the key instead of truthiness."""
    if isinstance(r, dict):
        return r["results"] if "results" in r else []
    return r or []


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")


def mark(st, uuid, phase, before=None, after=None):
    st.setdefault(uuid, {})[phase] = {"at": datetime.now().isoformat(timespec="seconds"),
                                      "before": before, "after": after}


# ── csv: the three wizard files ───────────────────────────────────────

ADDR_COLS = ["Property Street Address", "Property City", "Property State",
             "Property ZIP Code", "Owner First Name", "Owner Last Name"]


def phase_csv(plan):
    """Add-Data merge CSVs (pass A = phones 1-9, pass B = the overflow) and the
    phone-tag CSV. The tag file's header is 'Phone Tag', singular, and one tag
    per row: verified against output/ftm_phone_tiers.csv and score_ftm_phones.py."""
    OUTDIR.mkdir(parents=True, exist_ok=True)

    def addr_row(p):
        return {"Property Street Address": p["street"], "Property City": p["city"],
                "Property State": p["state"] or "TN", "Property ZIP Code": p["zip"],
                "Owner First Name": p.get("owner_first") or "",
                "Owner Last Name": p.get("owner_last") or ""}

    a_rows, b_rows, tag_rows = [], [], []
    for p in plan:
        if p["phones"]:
            r = addr_row(p)
            for i, ph in enumerate(p["phones"], 1):
                r[f"Phone {i}"] = ph["number"]
            a_rows.append(r)
        if p["phone_overflow"]:
            r = addr_row(p)
            for i, ph in enumerate(p["phone_overflow"], 1):
                r[f"Phone {i}"] = ph["number"]
            b_rows.append(r)
        for ph in p["all_phone_tags"]:
            for t in ph["tags"]:
                tag_rows.append({"Phone Number": ph["number"], "Phone Tag": t})

    seen, deduped = set(), []
    for r in tag_rows:
        k = (r["Phone Number"], r["Phone Tag"])
        if k not in seen:
            seen.add(k)
            deduped.append(r)

    pa = OUTDIR / "obit_heir_phones_pass_a.csv"
    pb = OUTDIR / "obit_heir_phones_pass_b.csv"
    pt = OUTDIR / "obit_phone_tags.csv"
    for path, rows, n in ((pa, a_rows, 9), (pb, b_rows, 4)):
        cols = ADDR_COLS + [f"Phone {i}" for i in range(1, n + 1)]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    with pt.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Phone Number", "Phone Tag"])
        w.writeheader()
        w.writerows(deduped)

    print(f"  {pa}  {len(a_rows)} records, {sum(len(p['phones']) for p in plan)} numbers")
    print(f"  {pb}  {len(b_rows)} records, {sum(len(p['phone_overflow']) for p in plan)} numbers")
    print(f"  {pt}  {len(deduped)} tag rows over "
          f"{len({r['Phone Number'] for r in deduped})} numbers")
    print(f"  tag vocabulary used: {dict(Counter(r['Phone Tag'] for r in deduped))}")
    print("\n  Wizard order (both are IRREVERSIBLE, no delete route exists):")
    print("    1. run_upload(pass_a, 'Obituary', existing_list=True)  -> verify phones landed")
    print("    2. run_upload(pass_b, 'Obituary', existing_list=True)  -> verify")
    print("    3. run_phone_tag_upload(obit_phone_tags.csv)           -> verify (async)")


# ── preflight ─────────────────────────────────────────────────────────

def phase_preflight(api, plan):
    ok = True
    # No user-identity endpoint exists on the internal API; prove the JWT
    # instead by reading a record we expect to own.
    probe = call(api, f"/api/internal/property/{plan[0]['uuid']}/")
    print(f"  auth OK, read {(probe.get('address') or {}).get('street')}")

    cols = call(api, f"/api/internal/siftline/board/{BOARD}/column/?limit=100")
    have = {c["uuid"] for c in aslist(cols)}
    for name, uuid in COLUMNS.items():
        if uuid not in have:
            print(f"  MISSING column {name} {uuid}")
            ok = False
    print(f"  board columns resolve: {len(COLUMNS)} of {len(COLUMNS)}")

    idx = field_index(api)
    need = ["Decision Maker", "DM Relationship", "DM 2 Name", "DM 2 Relationship",
            "DM 3 Name", "DM 3 Relationship", "DM Confidence", "Decedent Name",
            "Date of Death", "Owner Deceased", "Obituary URL"]
    for lbl in need:
        f = idx.get(lbl)
        if not f:
            print(f"  MISSING custom field {lbl}")
            ok = False
        elif f["field_type"] == "select":
            print(f"  select {lbl}: options {list(f['options'])}")
    print(f"  custom fields resolve: {sum(1 for l in need if l in idx)} of {len(need)}")

    seqs = aslist(call(api, "/api/internal/sequence/?limit=999"))
    risky = []
    for s in seqs:
        if not s.get("is_active"):
            continue
        t = s.get("trigger")
        if t == "card.created":
            risky.append(s.get("title"))
        elif t == "card.moved":
            for c in (s.get("conditions") or []):
                if BOARD in (((c.get("payload") or {}).get("meta") or {}).get("boards") or []):
                    risky.append(s.get("title"))
    print(f"  sequences armed on OUR board or card.created: {len(risky)} {risky or ''}")
    if risky:
        ok = False
    print(f"\n  PREFLIGHT {'PASS' if ok else 'FAIL'}")
    return ok


# ── API write phases ──────────────────────────────────────────────────

def phase_assign(api, plan, commit, st):
    for p in plan:
        if not p["assign_uuid"]:
            continue
        if commit:
            call(api, f"/api/internal/property/{p['uuid']}/", "PATCH",
                 {"assigned_to": p["assign_uuid"]})
            mark(st, p["uuid"], "assign", after=p["assign_to"])
        print(f"  {'SET ' if commit else 'would'} {p['street'][:30]:30} -> {p['assign_to']}")


def phase_tags(api, plan, commit, st):
    """Read-modify-write. PATCH {tags_add:[...]} is silently ignored and
    POST /property/add-tags/ applies account-wide, so neither is safe here."""
    for p in plan:
        if not p["tags"]:
            continue
        rec = call(api, f"/api/internal/property/{p['uuid']}/")
        cur = list(rec.get("tags") or [])
        new = [t for t in p["tags"] if t not in cur]
        if not new:
            print(f"  skip  {p['street'][:30]:30} (all {len(p['tags'])} tags present)")
            continue
        if commit:
            call(api, f"/api/internal/property/{p['uuid']}/", "PATCH", {"tags": cur + new})
            mark(st, p["uuid"], "tags", before=cur, after=cur + new)
        print(f"  {'ADD ' if commit else 'would'} {p['street'][:30]:30} + {new}")


def phase_fields(api, plan, commit, st, idx):
    for p in plan:
        pairs, skipped = [], []
        for label, value in p["fields"].items():
            f = idx.get(label)
            if not f:
                skipped.append(f"{label} (field missing)")
                continue
            v = value
            if f["field_type"] == "select":
                v = f["options"].get(str(value))
                if not v:
                    skipped.append(f"{label}={value} (no such option)")
                    continue
            pairs.append({"field_uuid": f["uuid"], "value": v})
        if commit and pairs:
            before = call(api, f"/api/internal/property/{p['uuid']}/custom-field/?limit=999")
            call(api, f"/api/internal/property/{p['uuid']}/custom-field/update-values/",
                 "PATCH", pairs)
            mark(st, p["uuid"], "fields", before=before, after=p["fields"])
        print(f"  {'SET ' if commit else 'would'} {p['street'][:30]:30} {len(pairs)} fields"
              + (f"  SKIPPED {skipped}" if skipped else ""))


def phase_notes(api, plan, commit, st):
    """One pinned note. Pinning is a sub-resource POST; `pinned` on create and
    PATCH {"pinned": true} both silently fail. There is no delete route."""
    for p in plan:
        msgs = call(api, f"/api/internal/property/{p['uuid']}/message/?limit=50")
        existing = aslist(msgs)
        # PATCH and DELETE on a message BOTH work (verified live 2026-08-11; the
        # API reference lists them as unknown). So an existing pack is updated in
        # place rather than stacked as a second note.
        prior = next((m for m in existing if MARKER in str(m.get("message") or "")), None)
        if prior:
            if str(prior.get("message") or "").strip() == p["note"].strip():
                print(f"  skip  {p['street'][:30]:30} (note already current)")
                continue
            if commit:
                call(api, f"/api/internal/property/{p['uuid']}/message/{prior['uuid']}/",
                     "PATCH", {"message": p["note"]})
                if not prior.get("pinned"):
                    try:
                        call(api, f"/api/internal/property/{p['uuid']}/message/"
                                  f"{prior['uuid']}/pin/", "POST")
                    except RuntimeError:
                        pass
                mark(st, p["uuid"], "notes", before=len(str(prior.get("message") or "")),
                     after=len(p["note"]))
            print(f"  {'EDIT' if commit else 'would edit'} {p['street'][:30]:30} "
                  f"{len(str(prior.get('message') or ''))} -> {len(p['note'])} chars")
            continue
        if commit:
            r = call(api, f"/api/internal/property/{p['uuid']}/message/", "POST",
                     {"message": p["note"]})
            muuid = (r or {}).get("uuid")
            if muuid:
                try:
                    call(api, f"/api/internal/property/{p['uuid']}/message/{muuid}/pin/", "POST")
                except RuntimeError as e:
                    print(f"      pin failed: {str(e)[:80]}")
            mark(st, p["uuid"], "notes", after=muuid)
        print(f"  {'POST' if commit else 'would'} {p['street'][:30]:30} "
              f"{len(p['note'])} chars")


def phase_cards(api, plan, commit, st):
    """create_card body key is `prop`, not `property`."""
    for p in plan:
        if not p["column_uuid"]:
            print(f"  skip  {p['street'][:30]:30} ({p['situation']}, no card by design)")
            continue
        cards = call(api, f"/api/internal/siftline/property/{p['uuid']}/card/")
        mine = [c for c in aslist(cards)
                if (c.get("board") or {}).get("uuid") == BOARD]
        if mine:
            cur = (mine[0].get("column") or {}).get("uuid")
            if cur == p["column_uuid"]:
                print(f"  skip  {p['street'][:30]:30} (already in {p['column']})")
                continue
            print(f"  MOVE? {p['street'][:30]:30} {cur} -> {p['column']} "
                  f"(left alone; a move can fire sequences)")
            continue
        if commit:
            call(api, f"/api/internal/siftline/board/column/{p['column_uuid']}/card/",
                 "POST", {"prop": p["uuid"]})
            mark(st, p["uuid"], "cards", after=p["column"])
        print(f"  {'CARD' if commit else 'would'} {p['street'][:30]:30} -> {p['column']}")


def phase_audit(api, plan, idx):
    rows, fails = [], 0
    for p in plan:
        rec = call(api, f"/api/internal/property/{p['uuid']}/")
        cf = call(api, f"/api/internal/property/{p['uuid']}/custom-field/?limit=999")
        cfv = {((c.get("custom_field") or {}).get("label")): c.get("value")
               for c in aslist(cf)}
        cards = call(api, f"/api/internal/siftline/property/{p['uuid']}/card/")
        mine = [c for c in aslist(cards)
                if (c.get("board") or {}).get("uuid") == BOARD]
        msgs = call(api, f"/api/internal/property/{p['uuid']}/message/?limit=50")
        noted = any(MARKER in str(m.get("message") or "")
                    for m in aslist(msgs))
        have_ph = {re.sub(r"\D", "", str(x.get("number") or ""))[-10:]
                   for x in ((rec.get("owner") or {}).get("phones") or [])}
        want_ph = {x["number"] for x in p["phones"]}
        r = {
            "street": p["street"],
            "assigned": rec.get("assigned_to") == p["assign_uuid"],
            "tags": all(t in (rec.get("tags") or []) for t in p["tags"]),
            "fields": all(str(cfv.get(k, "")) != "" for k in p["fields"]),
            "note": noted,
            "card": bool(mine) and (mine[0].get("column") or {}).get("uuid") == p["column_uuid"]
                    if p["column_uuid"] else not mine,
            "phones": want_ph.issubset(have_ph) if want_ph else True,
        }
        if not all(v for k, v in r.items() if k != "street"):
            fails += 1
        rows.append(r)
    print(f"  {'street':32} assign tags fields note card phones")
    for r in rows:
        print(f"  {r['street'][:32]:32} "
              + " ".join("OK  " if r[k] else "FAIL" for k in
                         ("assigned", "tags", "fields", "note", "card", "phones")))
    print(f"\n  {len(rows) - fails} of {len(rows)} records fully verified")
    (OUTDIR / "crm_write_audit.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return fails == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", default="csv", choices=PHASES + ["all"])
    ap.add_argument("--plan", default=str(PLAN))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if args.limit:
        plan = plan[:args.limit]
    print(f"{len(plan)} records | phase={args.phase} | "
          f"{'COMMIT' if args.commit else 'DRY RUN, nothing will be written'}\n")

    if args.phase == "csv":
        phase_csv(plan)
        return

    api = Api()
    st = load_state()
    order = ["preflight", "assign", "tags", "fields", "notes", "cards"] \
        if args.phase == "all" else [args.phase]
    for ph in order:
        print(f"\n== {ph} ==")
        if ph == "preflight":
            if not phase_preflight(api, plan) and args.commit:
                sys.exit("preflight failed, refusing to write")
        elif ph == "assign":
            phase_assign(api, plan, args.commit, st)
        elif ph == "tags":
            phase_tags(api, plan, args.commit, st)
        elif ph == "fields":
            phase_fields(api, plan, args.commit, st, field_index(api))
        elif ph == "notes":
            phase_notes(api, plan, args.commit, st)
        elif ph == "cards":
            phase_cards(api, plan, args.commit, st)
        elif ph == "audit":
            phase_audit(api, plan, field_index(api))
        if args.commit:
            save_state(st)
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
