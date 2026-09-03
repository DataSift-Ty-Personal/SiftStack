"""Push the FTM deep-prospecting v5 results into DataSift.

Two write surfaces, both idempotent:
  * per-phone tags  POST /api/internal/owner/{uuid}/upsert-phones/
  * a pinned research pack on the record's message board

THE OWNER RULE: a number the owner also holds carries its TIER only, never a
relationship label. Households share lines, and without this the dial sheet
labels the owner's own landline "Husband".

THE SPOUSE-OBITUARY TRAP: an obituary on a record does NOT mean the owner died.
Every record whose top heir is a spouse gets an explicit VERIFY line, because
the failure mode is asking a recent widow for her dead husband. Where the owner
reads alive and has a number, the pack says call the OWNER first.

SmartSkip's Deceased flag is advisory only: it carries no date of death and is
documented returning false for a man who died with a published obituary.

Message-board mechanics follow obituary_crm_push: pinning is a sub-resource
POST (`pinned` on create and PATCH both silently fail), and a prior pack is
edited in place rather than stacked as a second note.

    python src/ftm_deep_prospect_push.py                 # dry run
    python src/ftm_deep_prospect_push.py --commit
    python src/ftm_deep_prospect_push.py --limit 5 --commit
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datasift_api_upload import Api  # noqa: E402

MARKER = "[FTM deep prospecting v5]"
# USE THE TIER NAME VERBATIM. The first build shortened these to "Dial 1".."Dial 4",
# which reads fine to a human and is invisible to every consumer: sms_agent.crm
# matches phone tags by NAME against DIAL_TIERS = ("Dial First", "Dial Second",
# "Dial Third", "Dial Fourth", "Drop"), so a shortened tag is the same as no tag
# at all. It cost a whole texting run: all 1,135 candidates held as "dial tier
# untagged" on numbers that were scored, tagged and paid for.
TIER_TAG = {t: t for t in ("Dial First", "Dial Second", "Dial Third",
                           "Dial Fourth", "Drop")}
SPOUSE = {"Wife", "Husband", "Spouse"}


def load():
    heir = json.load(io.open("output/dp/ftm_heir_map.json", encoding="utf-8"))
    scores = {}
    p = "output/dp/ftm_trestle_scores.json"
    if os.path.exists(p):
        scores = {d["number"]: d for d in json.load(io.open(p, encoding="utf-8"))}
    phones = {r["uuid"]: r for r in
              json.load(io.open("output/dp/ftm_phones.json", encoding="utf-8"))}
    return heir, scores, phones


def dial_line(num, scores, label=""):
    s = scores.get(num) or {}
    sc = s.get("score")
    tier = s.get("tier") or "unscored"
    lt = (s.get("line_type") or "")[:9]
    pretty = "(%s) %s-%s" % (num[:3], num[3:6], num[6:]) if len(num) == 10 else num
    bits = [pretty, tier]
    if sc is not None:
        bits.append(str(sc))
    if lt:
        bits.append(lt)
    if label:
        bits.append(label)
    return "  " + "  ".join(bits)


def build_note(h, scores, phones):
    own_nums = [p["number"] for p in (phones.get(h["uuid"], {}) or {}).get("phones", [])]
    own_set = set(own_nums)
    alive = not h.get("smartskip_deceased")
    top = h["heirs"][0]["rel"] if h["heirs"] else None

    # Source names carry trailing punctuation on 23 of 490 records ("Pershing
    # Jones,"). Cosmetic, but this page is read aloud off a screen by a caller.
    owner = re.sub(r"[\s,;.]+$", "", h["owner"] or "")
    L = [MARKER, "Owner of record: %s" % owner, "Property: %s" % h["property"], ""]

    if top in SPOUSE:
        L += ["VERIFY BEFORE CALLING: the top relative is a SPOUSE. An obituary on",
              "this record does NOT prove the OWNER died, it is often the spouse's.",
              "Match the decedent name against the owner of record first. If the owner",
              "is living, this is a senior homeowner to call gently, not an heir case.",
              ""]
    if alive and own_nums:
        L += ["Owner reads LIVING and has a direct number. CALL THE OWNER FIRST;",
              "the heir map below is the fallback.", ""]
    elif h.get("smartskip_deceased"):
        L += ["SmartSkip flags the owner deceased. It carries NO date of death and is",
              "unreliable, so confirm against an obituary before working this as an",
              "estate.", ""]

    if own_nums:
        L.append("OWNER NUMBERS")
        ranked = sorted(own_nums,
                        key=lambda x: -((scores.get(x) or {}).get("score") or 0))
        for n in ranked:
            L.append(dial_line(n, scores))
        L.append("")

    if h["heirs"]:
        L.append("HEIR MAP (%d relatives, ranked by TN intestacy)" % len(h["heirs"]))
        for i, c in enumerate(h["heirs"][:8], 1):
            extra = []
            if c.get("age"):
                extra.append("age %s" % c["age"])
            if c.get("co_resident"):
                extra.append("SAME MAILING ADDRESS")
            line = " %d. %s - %s" % (i, c["name"], c["rel"])
            if extra:
                line += " (%s)" % ", ".join(extra)
            L.append(line)
            for n in c["phones"][:4]:
                if n in own_set:
                    L.append(dial_line(n, scores, "(also the owner's line)"))
                else:
                    L.append(dial_line(n, scores, c["rel"]))
        if len(h["heirs"]) > 8:
            L.append(" ... %d further relatives held in the heir map file"
                     % (len(h["heirs"]) - 8))
        L.append("")
    else:
        L += ["No relatives returned by SmartSkip. Needs obituary/web research before",
              "this record is callable as an estate.", ""]

    gen = sum(1 for c in h["heirs"] if c["rel"] in ("Relative", "In-Law"))
    if gen:
        L.append("NOTE: %d of %d relationships came back generic (In-Law/Relative)."
                 " SmartSkip's labels are coarse; the obituary is what establishes"
                 " who actually signs." % (gen, len(h["heirs"])))
    L.append("Source: SmartSkip bulk skip trace plus TrestleIQ scoring. Relationship"
             " labels are SmartSkip's own and are not verified against an obituary.")
    return "\n".join(L)


def phone_tags(h, scores, phones):
    """number -> tags. Owner numbers get a tier only (the owner rule)."""
    own = {p["number"] for p in (phones.get(h["uuid"], {}) or {}).get("phones", [])}
    out = {}
    for n in own:
        t = TIER_TAG.get((scores.get(n) or {}).get("tier") or "")
        if t:
            out[n] = [t]
    for c in h["heirs"][:3]:
        for n in c["phones"]:
            if n in own:
                continue
            t = TIER_TAG.get((scores.get(n) or {}).get("tier") or "")
            tags = [x for x in [c["rel"], t] if x]
            if tags:
                out[n] = tags
    return out


def upsert_phones(api, owner_uuid, tags, max_retries=6):
    """Write per-phone tags, dropping whatever number the API rejects.

    ONE bad number rejects the WHOLE batch, so a single unassigned area code
    silently costs a record all of its tags. DataSift's validation cannot be
    predicted from the outside (745-843-9015 is well-formed NANP and Trestle
    does not flag it, but the API refuses it), so rather than guess the rules
    we read the rejected INDEX out of the error body and retry without it.

    Returns (ok, n_dropped).
    """
    items = [{"number": n, "type": "UNKNOWN", "tags": t, "status": "UNKNOWN",
              "is_connected": True, "verified": False}
             for n, t in list(tags.items())[:20]]
    dropped = 0
    for _ in range(max_retries):
        if not items:
            return True, dropped
        try:
            api.call("/api/internal/owner/%s/upsert-phones/" % owner_uuid,
                     "POST", {"phones": items})
            return True, dropped
        except Exception as exc:
            msg = str(exc)
            idxs = sorted({int(m) for m in re.findall(r'"phones":\s*\{\s*"(\d+)"', msg)}
                          | {int(m) for m in re.findall(r"'phones':\s*\{\s*'(\d+)'", msg)})
            if not idxs:
                print("      upsert failed, not an index error: %s" % msg[:110])
                return False, dropped
            for i in reversed(idxs):
                if 0 <= i < len(items):
                    items.pop(i)
                    dropped += 1
    return False, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--skip-phones", action="store_true")
    ap.add_argument("--tag-all-owners", action="store_true",
                    help="tier-tag the non-probate records in the same queue")
    a = ap.parse_args()

    heir, scores, phones = load()
    if not scores:
        print("WARNING: no Trestle scores loaded; every tier will read 'unscored'.")
    api = Api()

    if a.tag_all_owners:
        # The heir map only covers probate/obituary, so the foreclosure, lien and
        # tax-delinquent records in the same call queue were scored but never
        # tagged. Tier tags only: there is no heir data for these, and the owner
        # rule means an owner's number never carries a relationship label anyway.
        done = {x["uuid"] for x in heir}
        rest = [r for r in phones.values() if r["uuid"] not in done]
        n_ok = n_drop = 0
        for i, r in enumerate(rest, 1):
            tags = {}
            for p in r["phones"]:
                t = TIER_TAG.get((scores.get(p["number"]) or {}).get("tier") or "")
                if t:
                    tags[p["number"]] = [t]
            if not (tags and r.get("owner_uuid")):
                continue
            if a.commit:
                ok, dropped = upsert_phones(api, r["owner_uuid"], tags)
                n_ok += 1 if ok else 0
                n_drop += dropped
            else:
                n_ok += 1
            if i % 50 == 0:
                print("  %d/%d" % (i, len(rest)), flush=True)
        print("\n%s tier tags on %d non-probate records, %d numbers refused"
              % ("wrote" if a.commit else "would write", n_ok, n_drop))
        if not a.commit:
            print("DRY RUN - nothing written.")
        return 0
    todo = heir[:a.limit] if a.limit else heir
    n_note = n_ph = n_skip = n_bad = 0

    for i, h in enumerate(todo, 1):
        note = build_note(h, scores, phones)
        try:
            msgs = api.call("/api/internal/property/%s/message/?limit=50" % h["uuid"])
            existing = msgs.get("results") if isinstance(msgs, dict) else msgs
            prior = next((m for m in (existing or [])
                          if MARKER in str(m.get("message") or "")), None)
            if prior and str(prior.get("message") or "").strip() == note.strip():
                n_skip += 1
            elif a.commit:
                if prior:
                    api.call("/api/internal/property/%s/message/%s/"
                             % (h["uuid"], prior["uuid"]), "PATCH", {"message": note})
                else:
                    r = api.call("/api/internal/property/%s/message/" % h["uuid"],
                                 "POST", {"message": note})
                    mu = (r or {}).get("uuid")
                    if mu:
                        try:
                            api.call("/api/internal/property/%s/message/%s/pin/"
                                     % (h["uuid"], mu), "POST")
                        except RuntimeError:
                            pass
                n_note += 1
            else:
                n_note += 1
        except Exception as exc:
            print("  note %s: %s: %s"
                  % (h["uuid"][:8], type(exc).__name__, str(exc)[:70]))

        if not a.skip_phones:
            tags = phone_tags(h, scores, phones)
            ow = (phones.get(h["uuid"]) or {}).get("owner_uuid")
            if tags and ow and a.commit:
                ok, dropped = upsert_phones(api, ow, tags)
                if ok:
                    n_ph += 1
                    n_bad += dropped
                else:
                    print("  phones %s: gave up after retries" % h["uuid"][:8])
            elif tags:
                n_ph += 1

        if i % 50 == 0:
            print("  %d/%d" % (i, len(todo)), flush=True)
            time.sleep(0.3)

    verb = "wrote" if a.commit else "would write"
    print("\n%s: %d notes, %d phone-tag calls, %d already current, "
          "%d numbers the API refused" % (verb, n_note, n_ph, n_skip, n_bad))
    if not a.commit:
        print("DRY RUN - nothing written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
