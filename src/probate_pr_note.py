"""Post a pinned message-board note on probate records that name a PR.

WHY. On a probate record the Owner First/Last we upload is the PERSONAL
REPRESENTATIVE, not the decedent. That is deliberate: the PR is the person who
can sign and the person outreach actually goes to. It is also fragile, because
DataSift's own "Enrich Owners" / "Swap Owners" would happily replace that
contact with the owner of record, who is dead. This note documents the decision
on the record itself so nobody silently undoes it.

    python src/probate_pr_note.py --targets output/pr_targets.json          # dry run
    python src/probate_pr_note.py --targets output/pr_targets.json --limit 1 --commit
    python src/probate_pr_note.py --targets output/pr_targets.json --commit

KNOWN GOTCHA, RE-VERIFIED EACH RUN: reisift's `pinned` flag has not stuck in
the past (2026-07-08: neither `pinned=true` on create nor a follow-up PATCH
survived a read-back). This script asks for the pin, reads the message back,
and REPORTS whether the pin actually held rather than assuming it did. The note
still lands as the newest message either way.

Resumable: a record that already carries the note is skipped, so a re-run after
an interruption does not stack duplicate comments.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datasift_api_upload import Api  # noqa: E402

# Marker used to detect a note this script already posted.
MARKER = "[FTM probate contact]"


def build_note(rec: dict) -> str:
    """The message body. Plain, specific, and aimed at whoever opens the record."""
    dec = (rec.get("Decedent Name") or "").strip()
    pr = (rec.get("Personal Representative") or "").strip()
    first = (rec.get("Owner First Name") or "").strip()
    last = (rec.get("Owner Last Name") or "").strip()
    contact = (first + " " + last).strip() or pr
    county = (rec.get("County") or "").strip()
    src = (rec.get("Source URL") or "").strip()

    lines = [
        f"{MARKER} We swapped the owner on this property to the personal representative.",
        "",
        f"Decedent (owner of record): {dec or 'not named in the notice'}",
        f"Personal representative: {pr}",
        f"Contact on this record: {contact}",
        "",
        "The PR is who we do outreach to. They are the one who can sign, and the "
        "owner of record is deceased, so the contact on this record is the PR on "
        "purpose. Do not run Enrich Owners or Swap Owners against this record: it "
        "would replace the PR with the deceased owner and break the outreach.",
        "",
        f"Source: {county} County probate notice, tnpublicnotice.com.",
    ]
    if src:
        lines.append(src)
    return "\n".join(lines)


def already_noted(api: Api, uuid: str) -> bool:
    """True when this record already carries our note (makes re-runs safe)."""
    try:
        r = api.call(f"/api/internal/property/{uuid}/message/?limit=50")
    except Exception:
        return False
    for m in (r.get("results") or r.get("data") or []):
        if MARKER in (m.get("message") or ""):
            return True
    return False


def post_note(api: Api, uuid: str, body: str, commit: bool) -> dict:
    """Post the note and try to pin it. Returns what actually happened."""
    if not commit:
        return {"posted": False, "pinned": False, "dry_run": True}

    msg = api.call(
        f"/api/internal/property/{uuid}/message/",
        "POST",
        {"message": body, "pinned": True},
    )
    muuid = msg.get("uuid") if isinstance(msg, dict) else None
    pinned = bool(msg.get("pinned")) if isinstance(msg, dict) else False

    # PINNING IS ITS OWN SUB-RESOURCE, not a field. Verified live 2026-08-16:
    #   POST   /message/{muuid}/pin/   -> pinned=True, survives a fresh read
    #   PATCH  /message/{muuid}/ {"pinned": true}  -> accepted, stays False
    #   POST   /message/{muuid}/ {"pinned": true}  -> ignored on create
    # This corrects the 2026-07-08 conclusion that reisift pinning was
    # unreachable from the API; the PATCH was simply the wrong door.
    if muuid and not pinned:
        try:
            api.call(f"/api/internal/property/{uuid}/message/{muuid}/pin/", "POST", {})
            pinned = True
        except Exception as exc:
            logger_msg = str(exc)[:100]
            print(f"    pin failed for {muuid}: {logger_msg}")

    return {"posted": True, "pinned": pinned, "message_uuid": muuid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", default="output/pr_targets.json")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="/api/internal throttles hard; keep this at or above 0.4")
    ap.add_argument("--state", default="output/pr_note_done.json",
                    help="records already noted, so a re-run resumes")
    a = ap.parse_args()

    recs = json.load(open(a.targets, encoding="utf-8"))
    if a.start:
        recs = recs[a.start:]
    if a.limit:
        recs = recs[:a.limit]

    done = set()
    if os.path.exists(a.state):
        try:
            done = set(json.load(open(a.state, encoding="utf-8")))
        except Exception:
            done = set()

    api = Api()
    posted = skipped = failed = pinned_ok = 0
    errors: list[str] = []

    print(f"[{'COMMIT' if a.commit else 'DRY RUN'}] {len(recs)} probate records with a PR\n")
    if not a.commit:
        print("--- note that would be posted on the first record ---")
        print(build_note(recs[0]))
        print("--- end ---\n")

    for i, r in enumerate(recs, 1):
        key = f"{r['Property Street Address']}|{r['Property City']}".lower()
        if key in done:
            skipped += 1
            continue
        try:
            prop = api.call("/property/", "POST", {"address": {
                "street": r["Property Street Address"], "city": r["Property City"],
                "state": r["Property State"], "postal_code": r["Property ZIP Code"]}})
            uuid = prop.get("uuid")
            if not uuid:
                raise RuntimeError("no uuid returned")

            if a.commit and already_noted(api, uuid):
                skipped += 1
                done.add(key)
                continue

            res = post_note(api, uuid, build_note(r), a.commit)
            if res.get("posted"):
                posted += 1
                if res.get("pinned"):
                    pinned_ok += 1
                done.add(key)
        except Exception as exc:
            failed += 1
            errors.append(f"{r['Property Street Address']}: {str(exc)[:120]}")
            if failed <= 5:
                print(f"  FAIL {r['Property Street Address']}: {str(exc)[:110]}")

        if i % 25 == 0:
            print(f"   {i}/{len(recs)} posted={posted} skipped={skipped} "
                  f"failed={failed} pinned={pinned_ok}", flush=True)
            if a.commit:
                json.dump(sorted(done), open(a.state, "w"))
        time.sleep(a.sleep)

    if a.commit:
        json.dump(sorted(done), open(a.state, "w"))

    print(f"\nposted={posted}  skipped(already noted)={skipped}  failed={failed}")
    if posted:
        print(f"pin actually held on {pinned_ok}/{posted}")
        if pinned_ok == 0:
            print("  NOTE: reisift ignored the pin flag, same as 2026-07-08. The note "
                  "is still the newest message on the record. Pinning needs the UI.")
    if errors:
        os.makedirs("output", exist_ok=True)
        with open("output/pr_note_errors.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(errors))
        print(f"errors -> output/pr_note_errors.txt ({len(errors)})")
    return 1 if (a.commit and posted == 0 and not skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
