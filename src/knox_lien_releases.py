"""Keep DataSift's lien numbers true after a lien is RELEASED (Ty, 2026-11-11).

"Anytime a lien is actually released, come back in and update that to zero."
Until this existed a lien was a one-way write: the upload stated what was owed
on the day it ran and nothing ever revisited it. Eleven weeks after the August
upload, 148 debtors had liens paid off (127 of them every lien) and their
records still read as owing.

HOW A RELEASE IS KNOWN. When a lien is paid, the creditor records a release at
the Register of Deeds, and that document names the instrument number of the
lien it clears. The daily pull collects releases; `knox_lien_index` keeps the
running set. This module compares that set against a LEDGER of the lien
records we put in DataSift and rewrites the ones that changed.

THE LEDGER (`lien_records.json`, in the scratch folder) holds, per DataSift
record: every lien instrument we know sits on that property, across every
debtor named on it and every pull, plus what DataSift currently SHOWS. A
record is rewritten whenever the truth differs from what is shown:

    truth  = instruments on the record that are not released, and their sum
    shown  = the active count, total count and dollar figure last written

One rule covers both cases that leave a record wrong:
  * a release was recorded (the number must go down, to zero when all are gone)
  * a later upload restated the record from a single month's filings, so a
    debtor with five old liens and one new one reads "1 of 1"

What a rewrite touches, and nothing else: Outstanding Lien Amount, Liens Active
vs Total, Total Delinquency (liens plus the tax figure already on the record),
Debt vs Equity Pct, the lien amount band tag, and a dated note. When every lien
is released the record gets the tag "Liens Released" and STAYS on the Liens
list: taking it off the list is a marketing decision, not a data correction.

    python src/knox_lien_releases.py --scratch output/knox --reconcile            # dry: what would change
    python src/knox_lien_releases.py --scratch output/knox --reconcile --commit
    python src/knox_lien_releases.py --scratch output/knox --reconcile --commit --limit 1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEDGER = "lien_records.json"
UNLISTED = "lien_unlisted.json"     # uuids already taken off the Liens list
BANDS =("lien_under_5k", "lien_5k_15k", "lien_15k_50k", "lien_50k_100k", "lien_over_100k")
RELEASED_TAG = "Liens Released"
LIST_TITLE = "Liens"
CRM_GAP = 1.25        # seconds between DataSift calls; /api/internal/ throttles hard
MAX_DEBTORS = 3       # more debtor names than this on one record: leave it alone

_DEBTOR = re.compile(r"Lien debtor (.+?): \d+ lien instrument")


# ── pure bookkeeping ─────────────────────────────────────────────────


def debtors_from_notes(notes: str) -> list[str]:
    """Every debtor a record's notes name ("Lien debtor DOE PAT: 2 lien instrument(s)...")."""
    return list(dict.fromkeys(m.group(1).strip() for m in _DEBTOR.finditer(notes or "")))


def band(amount: float) -> str:
    for hi, label in ((5000, BANDS[0]), (15000, BANDS[1]), (50000, BANDS[2]), (100000, BANDS[3])):
        if amount < hi:
            return label
    return BANDS[4]


def truth(entry: dict, released: set, amounts: dict) -> dict:
    """What the record SHOULD show, from everything known to sit on it."""
    every = sorted({i for ins in (entry.get("debtors") or {}).values() for i in ins})
    active = [i for i in every if i not in released]
    return {"active": len(active), "total": len(every),
            "amount": round(sum(float(amounts.get(i, 0) or 0) for i in active), 2),
            "active_instruments": active}


def add_debtor(ledger: dict, uuid: str, street: str, zip5: str, debtor: str,
               instruments: list[str]) -> dict:
    """Record that these instruments sit on this DataSift record. Accumulates."""
    entry = ledger.setdefault(uuid, {"street": street, "zip": zip5, "debtors": {}, "shown": None})
    held = set(entry["debtors"].get(debtor) or [])
    entry["debtors"][debtor] = sorted(held | set(instruments or []))
    return entry


def set_shown(entry: dict, active: int, total: int, amount: float) -> None:
    entry["shown"] = {"active": int(active), "total": int(total), "amount": round(float(amount or 0), 2)}


def plan(ledger: dict, released: set, amounts: dict) -> list[dict]:
    """Records whose shown lien numbers no longer match the truth."""
    changes = []
    for uuid, entry in ledger.items():
        # Liens reach a parcel through a NAME match, and a common name collects
        # namesakes. A record carrying this many different debtor names is more
        # likely a pile-up than a household, so its total is never summed.
        if len(entry.get("debtors") or {}) > MAX_DEBTORS:
            continue
        now = truth(entry, released, amounts)
        shown = entry.get("shown") or {}
        same = (shown.get("active") == now["active"] and shown.get("total") == now["total"]
                and abs(float(shown.get("amount") or 0) - now["amount"]) < 0.01)
        if same:
            continue
        every = {i for ins in entry["debtors"].values() for i in ins}
        changes.append({
            "uuid": uuid, "street": entry.get("street", ""), "debtors": sorted(entry["debtors"]),
            "before": dict(shown), "after": {k: now[k] for k in ("active", "total", "amount")},
            "released": sorted(i for i in every if i in released),
            "fully_released": now["active"] == 0,
        })
    return changes


def note_for(change: dict, today: str) -> str:
    after, before = change["after"], change.get("before") or {}
    went = (before.get("active") or 0) - after["active"]
    if went > 0:
        lead = "%d lien(s) released at the Register of Deeds since this was last stated." % went
    else:
        lead = "Lien figures restated from every filing on record."
    return ("Lien update %s: %s %d of %d lien(s) still active. Outstanding lien total now $%s."
            % (today, lead, after["active"], after["total"], format(after["amount"], ",.2f")))


# ── files ────────────────────────────────────────────────────────────


def _read(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def load_ledger(scratch: str) -> dict:
    return _read(os.path.join(scratch, LEDGER), {})


def save_ledger(scratch: str, ledger: dict) -> None:
    _write(os.path.join(scratch, LEDGER), ledger)


def load_released(scratch: str) -> set:
    return set((_read(os.path.join(scratch, "lien_releases.json"), {}) or {})
               .get("released_instruments") or [])


def load_amounts(scratch: str) -> dict:
    return _read(os.path.join(scratch, "lien_amounts.json"), {}) or {}


# ── DataSift ─────────────────────────────────────────────────────────


def _num(value) -> float | None:
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _crm(api, path, method="GET", body=None, headers=None):
    import datasift_api_upload as up

    for attempt in range(6):
        try:
            time.sleep(CRM_GAP)
            return api.call(path, method, body, headers=headers)
        except up.ApiError as exc:
            if getattr(exc, "code", 0) not in (429, 502, 503, 504):
                raise
            hint = re.search(r"available in (\d+)", str(exc))
            time.sleep(int(hint.group(1)) + 2 if hint else 10 * (attempt + 1))
    raise RuntimeError("gave up on " + path)


def _fields(api, uuid: str) -> dict:
    got = _crm(api, "/api/internal/property/%s/custom-field/?limit=1000" % uuid)
    rows = got.get("results", got) if isinstance(got, dict) else got
    return {((x.get("custom_field") or {}).get("label") or ""): x.get("value") for x in rows or []}


def apply_change(api, idx: dict, change: dict, today: str) -> dict:
    """Rewrite one record's lien figures and read them back. Returns a result row."""
    import datasift_api_upload as up

    uuid = change["uuid"]
    rec = _crm(api, "/api/internal/property/%s/" % uuid)
    named = debtors_from_notes(str(rec.get("notes") or ""))
    # The ledger was joined to records by street. Before writing a dollar figure,
    # require the record itself to name one of these debtors.
    if not any(d in named for d in change["debtors"]):
        return {"uuid": uuid, "status": "skipped", "why": "record's notes do not name this debtor"}

    have = _fields(api, uuid)
    after = change["after"]
    # `shown` in the ledger is our belief about the record. If the record
    # already reads true (the belief was stale, or someone fixed it by hand)
    # there is nothing to write and no note to add.
    if (str(have.get("Liens Active vs Total") or "") == "%d of %d" % (after["active"], after["total"])
            and abs((_num(have.get("Outstanding Lien Amount")) or 0.0) - after["amount"]) < 0.01):
        return {"uuid": uuid, "status": "already_true"}
    tax = _num(have.get("Tax delinquency amount")) or 0.0
    equity = _num(have.get("Est Equity"))
    total = round(after["amount"] + tax, 2)
    values = {
        "Outstanding Lien Amount": after["amount"],
        "Liens Active vs Total": "%d of %d" % (after["active"], after["total"]),
        "Total Delinquency": total,
    }
    if equity and equity > 0:
        values["Debt vs Equity Pct"] = round(100 * total / equity, 1)
    pairs = [{"field_uuid": idx[label]["uuid"], "value": str(value)}
             for label, value in values.items() if label in idx]
    warn: set = set()
    time.sleep(CRM_GAP)
    landed = up.write_fields(api, uuid, pairs, warn)

    # Tags: a PATCH of the tag list only ever ADDS (verified live 2026-11-11: the
    # old band tag survived a PATCH that left it out), so a tag that no longer
    # applies has to go through remove-tags, which takes TITLES.
    current = up._names(rec.get("tags"))
    keep = ([band(after["amount"])] if after["amount"] > 0 else []) + \
           ([RELEASED_TAG] if change["fully_released"] else [])
    drop = [t for t in current if (t in BANDS or t == RELEASED_TAG) and t not in keep]
    add = [t for t in keep if t not in current]
    if drop:
        _crm(api, "/api/internal/property/%s/remove-tags/" % uuid, "POST", {"tags": drop})
    if add:
        _crm(api, "/api/internal/property/%s/" % uuid, "PATCH",
             {"tags": [t for t in current if t not in drop] + add})
    try:
        _crm(api, "/api/internal/property/%s/add-notes/" % uuid, "POST",
             {"notes": note_for(change, today)})
    except Exception as exc:   # the figures are what matter; the note is the audit trail
        warn.add("note not added: %s" % str(exc)[:80])

    back = _fields(api, uuid)
    ok = (abs((_num(back.get("Outstanding Lien Amount")) or 0.0) - after["amount"]) < 0.01
          and str(back.get("Liens Active vs Total") or "") == values["Liens Active vs Total"])
    return {"uuid": uuid, "status": "updated" if ok else "mismatch", "fields": landed,
            "before": change.get("before"), "after": after, "warn": sorted(warn),
            "was_amount": _num(have.get("Outstanding Lien Amount"))}


def remove_from_list(api, uuid: str) -> bool:
    """Take one record off the Liens list and confirm it is gone.

    Ty, 2026-11-11: a record whose liens are ALL released is no longer a lien
    lead and comes off the lead list. Per-record route, and `lists` is a STRING
    (an array returns 201 and does nothing, the strip_bulk_lists contract). Only
    the Liens list is touched: the record may sit on Probate or Foreclosure for
    a reason a lien release does not change.
    """
    import datasift_api_upload as up

    _crm(api, "/api/internal/property/%s/remove-lists/" % uuid, "POST", {"lists": LIST_TITLE})
    rec = _crm(api, "/api/internal/property/%s/" % uuid)
    return LIST_TITLE not in up._names(rec.get("lists"))


def unlist_released(scratch: str, api=None, commit: bool = False, limit: int = 0, log=print) -> dict:
    """Take every fully released lien record off the Liens list.

    Catches records that were zeroed before this rule existed, or whose list
    removal failed. Two checks before touching a record: its notes name one of
    the ledger's debtors, and the record ITSELF already reads zero active
    liens. A record still showing an open lien is left for reconcile first.
    """
    import datasift_api_upload as up

    ledger = load_ledger(scratch)
    released, amounts = load_released(scratch), load_amounts(scratch)
    path = os.path.join(scratch, UNLISTED)
    done = set(_read(path, []))
    todo = [uuid for uuid, entry in ledger.items()
            if uuid not in done and len(entry.get("debtors") or {}) <= MAX_DEBTORS
            and (now := truth(entry, released, amounts))["total"] > 0 and now["active"] == 0]
    stats = {"fully_released": len(todo) + len(done & set(ledger)), "to_unlist": len(todo),
             "unlisted": 0, "not_on_list": 0, "not_ready": 0, "skipped": 0, "failed": 0,
             "other_lists": {}}
    if not commit or not todo:
        return stats
    if limit:
        todo = todo[:limit]
    api = api or up.Api()
    for n, uuid in enumerate(todo, 1):
        try:
            rec = _crm(api, "/api/internal/property/%s/" % uuid)
            named = debtors_from_notes(str(rec.get("notes") or ""))
            if not any(d in named for d in ledger[uuid]["debtors"]):
                stats["skipped"] += 1
                done.add(uuid)                      # not ours to change; do not ask again
            elif not str(_fields(api, uuid).get("Liens Active vs Total") or "").startswith("0 of"):
                stats["not_ready"] += 1             # reconcile has not zeroed it yet
                continue
            else:
                lists = up._names(rec.get("lists"))
                for title in lists:
                    if title != LIST_TITLE:
                        stats["other_lists"][title] = stats["other_lists"].get(title, 0) + 1
                if LIST_TITLE not in lists:
                    stats["not_on_list"] += 1
                    done.add(uuid)
                elif remove_from_list(api, uuid):
                    stats["unlisted"] += 1
                    done.add(uuid)
                else:
                    stats["failed"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log("  unlist failed for %s: %s: %s" % (uuid[:8], type(exc).__name__, str(exc)[:120]))
        _write(path, sorted(done))
        if n % 25 == 0:
            log("  off the Liens list %d/%d (removed=%d)" % (n, len(todo), stats["unlisted"]))
    return stats


def reconcile(scratch: str, api=None, commit: bool = False, limit: int = 0, log=print) -> dict:
    """Rewrite every lien record whose shown figures are no longer true."""
    ledger = load_ledger(scratch)
    released, amounts = load_released(scratch), load_amounts(scratch)
    changes = plan(ledger, released, amounts)
    stats = {"records": len(ledger), "to_update": len(changes),
             "fully_released": sum(1 for c in changes if c["fully_released"]),
             "updated": 0, "already_true": 0, "skipped": 0, "mismatch": 0, "failed": 0,
             "committed": commit, "unlisted": 0}
    if not commit:
        return stats
    if limit:
        changes = changes[:limit]

    import datasift_api_upload as up

    api = api or up.Api()
    idx = up.field_index(api) if changes else {}
    today = date.today().isoformat()
    results_path = os.path.join(scratch, "lien_release_updates.jsonl")
    for n, change in enumerate(changes, 1):
        try:
            res = apply_change(api, idx, change, today)
        except Exception as exc:
            res = {"uuid": change["uuid"], "status": "failed", "why": "%s: %s" % (type(exc).__name__, str(exc)[:160])}
        res["day"] = today
        stats[res["status"]] = stats.get(res["status"], 0) + 1
        with open(results_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(res) + "\n")
        if res["status"] in ("updated", "already_true", "skipped"):
            # A skipped record is settled too: it is not ours to rewrite, and
            # asking again tomorrow would only spend the same calls.
            set_shown(ledger[change["uuid"]], **change["after"])
            save_ledger(scratch, ledger)
        if n % 25 == 0:
            log("  lien updates %d/%d (updated=%d skipped=%d failed=%d)" % (
                n, len(changes), stats["updated"], stats["skipped"], stats["failed"] + stats["mismatch"]))

    # A record whose liens are all released is no longer a lien lead: off the
    # Liens list it comes. A separate sweep, so anything zeroed earlier (or
    # whose removal failed) is caught on the next run too.
    if not limit:
        off = unlist_released(scratch, api=api, commit=True, log=log)
        stats["unlisted"] = off["unlisted"]
        stats["failed"] += off["failed"]
    return stats


def register_uploaded(scratch: str, csv_path: str, api=None) -> int:
    """After a successful upload: remember which liens now sit on which record.

    Must run BEFORE `knox_liens_daily.mark_uploaded`, which empties the staged
    debtor file this reads the full instrument lists from.
    """
    import datasift_api_upload as up

    from address_fallback import street_key

    staged_list = [d for d in _read(os.path.join(scratch, "knox_lien_parcels.json"), [])
                   if d.get("n_parcels") and not d.get("institutional")]
    staged = {d.get("name"): d for d in staged_list}
    with open(csv_path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("Lists") or "") == LIST_TITLE]
    if not rows:
        return 0
    api = api or up.Api()
    ledger = load_ledger(scratch)
    added = 0
    relisted: set = set()
    for row in rows:
        names = debtors_from_notes(row.get("Notes") or "")
        if not names or names[0] not in staged:
            continue
        street = row.get("Property Street Address") or ""
        time.sleep(CRM_GAP)
        uuid = up.find_property(api, {"street": street,
                                      "postal_code": row.get("Property ZIP Code") or ""})
        if not uuid:
            continue
        # The upload keeps one row per house (the largest lien). A co-debtor
        # with a parcel at the same street still has liens on it, so every
        # staged debtor there goes into the ledger, not just the row's.
        want = street_key(street)
        here = [d for d in staged_list
                if d["name"] == names[0]
                or any(street_key(p.get("address") or "") == want for p in d.get("parcels") or [])]
        for debtor in here:
            entry = add_debtor(ledger, uuid, street, (row.get("Property ZIP Code") or "")[:5],
                               debtor["name"],
                               debtor.get("all_instruments") or debtor.get("instruments") or [])
        active, _, total = (row.get("Liens Active vs Total") or "0 of 0").partition(" of ")
        set_shown(entry, int(active or 0), int(total or 0), _num(row.get("Outstanding Lien Amount")) or 0.0)
        relisted.add(uuid)
        added += 1
    save_ledger(scratch, ledger)
    # A new lien on a record that had been fully released: the upload just put
    # it back on the Liens list, so it may be taken off again one day.
    path = os.path.join(scratch, UNLISTED)
    off = set(_read(path, []))
    if off & relisted:
        _write(path, sorted(off - relisted))
    return added


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scratch", default=os.environ.get("FTM_SCRATCH", "output/knox"))
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--unlist-released", action="store_true",
                    help="take fully released records off the Liens list")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    if a.unlist_released:
        stats = unlist_released(a.scratch, commit=a.commit, limit=a.limit)
        for key, value in stats.items():
            print("%-16s %s" % (key, value))
        if not a.commit:
            print("\nNothing written. Re-run with --commit.")
        return 1 if stats["failed"] else 0
    if not a.reconcile:
        ap.print_help()
        return 0
    stats = reconcile(a.scratch, commit=a.commit, limit=a.limit)
    for key, value in stats.items():
        print("%-16s %s" % (key, value))
    if not a.commit:
        print("\nNothing written. Re-run with --commit.")
    return 1 if (stats["failed"] or stats["mismatch"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
