"""How many Priority 1 contacts can still get a FIRST touch, measured, not estimated.

Everyone in the Smarter Contact source group, minus everyone already sent
Touch 1, checked against the same do-not-text sources as the follow-up audit
(smarter_contact_audit.py): Smarter Contact suppressions, the SMS agent's
suppression table, and Sift (record status, the DNC list, the Do Not Market
tag, the phone's own status, and the registry do-not-call flag where Sift
exposes it, which is only on a record's representative phone), plus Zillow:
a house agent-listed right now is not texted (mls_listings).

Read-only. Writes output/smarter_contact/audits/remaining_<time>.json with the
clean contact ids per reason, ready to build the next waves from.

    python src/smarter_contact_remaining.py --suppression fly_suppression.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sms_agent import config, crm, gate_rules, sc_client as C, store  # noqa: E402
from mls_listings import ActiveListings  # noqa: E402
from smarter_contact_audit import (OPEN_STATUSES, SKIP_PHONE, OUT, _names, _status, gate_extra,  # noqa: E402,F401
                                   suppressions)

SOURCE_GROUP = "Priority 1- Attempt 1"
TEXTED_GROUPS = ("P1 Wave 1 East", "P1 Wave 1 Other TZ", "P1 No Name East", "P1 No Name Other TZ")
Q_GROUPS = ("query G($p: Pagination) { findGroups(pagination: $p, filter: {}) { items { id name contacts } "
            "hasNext nextId nextCreatedAt } }")
Q_CONTACTS = ("query F($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) { hasNext nextId "
              "nextCreatedAt items { id phone attrs { fieldId value } } } }")


def _all(query: str, key: str, variables: dict) -> list[dict]:
    rows, page = [], {"limit": 100}
    while True:
        data = C.gql(query, dict(variables, p=page))[key]
        rows += data["items"]
        if not data.get("hasNext"):
            return rows
        page = {"limit": 100, "nextId": data["nextId"], "nextCreatedAt": data["nextCreatedAt"]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--suppression", default="", help="JSON {phone: reason} exported from the Fly agent")
    a = ap.parse_args()
    groups = {g["name"]: g for g in _all(Q_GROUPS, "findGroups", {})}
    apn = next(f["id"] for f in C.gql("query { findFields { id key } }")["findFields"]
               if f["key"] == "ASSESSOR_PARCEL_NUMBER")
    source = _all(Q_CONTACTS, "findContacts", {"f": {"status": "ACTIVE", "groups": [groups[SOURCE_GROUP]["id"]]}})
    texted = set()
    for name in TEXTED_GROUPS:
        texted |= {c["id"] for c in _all(Q_CONTACTS, "findContacts",
                                         {"f": {"status": "ACTIVE", "groups": [groups[name]["id"]]}})}
    remaining = [c for c in source if c["id"] not in texted]
    print(f"source group {len(source)} | already sent Touch 1 {len(texted)} | not yet texted {len(remaining)}",
          flush=True)

    sc_supp = suppressions()
    agent_supp = json.loads(Path(a.suppression).read_text(encoding="utf-8")) if a.suppression else {}
    # Representative phone + status for every Priority 1 record, in bulk.
    p1 = crm.fetch_cohort({"any_tags": crm.tag_uuids(["Priority 1"])})
    rep = {r["uuid"]: ((r.get("phone") or {}), _status(r)) for r in p1}
    print(f"Sift Priority 1 records {len(p1)} | SC suppressions {len(sc_supp)} | agent suppressions {len(agent_supp)}",
          flush=True)

    listings = ActiveListings()
    details: dict[str, dict] = {}
    reasons, clean, per_reason = Counter(), [], {}
    gate_would = Counter()      # report mode: what the suppression gate would also exclude
    for i, ct in enumerate(remaining, 1):
        phone = store.clean_phone(ct["phone"])
        record = next((x["value"] for x in ct.get("attrs") or [] if x["fieldId"] == apn), "")
        why = []
        if phone in sc_supp:
            why.append(f"sc_suppressed:{sc_supp[phone]}")
        if phone in agent_supp:
            why.append(f"agent_suppressed:{agent_supp[phone].split(':')[0]}")
        if not record:
            why.append("no_sift_record_id")
        else:
            if record not in details:
                details[record] = crm.get_record(record, fresh=True) or {}
            rec = details[record]
            if not rec:
                why.append("sift_record_missing")
            else:
                st = _status(rec)
                if st not in OPEN_STATUSES:
                    why.append(f"sift_status:{st}")
                if config.LIST_DNC in _names(rec.get("lists")):
                    why.append("sift_dnc_list")
                if config.TAG_OPT_OUT in _names(rec.get("tags")):
                    why.append("sift_do_not_market")
                obj = next((p for p in ((rec.get("owner") or {}).get("phones") or [])
                            if store.clean_phone(p.get("number")) == phone), None)
                pst = ((obj or {}).get("status") or "").upper()
                # One rule decides whether a phone may be texted (sms_agent/gate_rules.do_not_text):
                # the phone's own status here, the do-not-call registry flag below.
                if gate_rules.do_not_text({"status": pst}):
                    why.append(f"sift_phone:{pst}")
                addr = rec.get("address") or {}
                if listings.lookup(addr.get("street") or "", addr.get("postal_code") or ""):
                    why.append("mls_listed")
                says = gate_extra(rec)          # '' unless SMS_AGENT_GATE is on
                if says and config.GATE_MODE == "enforce":
                    why.append(f"gate:{says}")
                elif says:
                    gate_would[says] += 1
            rp, _ = rep.get(record, ({}, ""))
            if store.clean_phone(rp.get("number")) == phone and gate_rules.do_not_text(rp) == gate_rules.REGISTRY:
                why.append("registry_dnc")
        if why:
            first = why[0].split(":")[0] if not why[0].startswith("sift_status") else why[0]
            reasons[first] += 1
            per_reason.setdefault(first, []).append(ct["id"])
        else:
            clean.append({"contact_id": ct["id"], "record": record})
        if i % 500 == 0:
            print(f"  checked {i}/{len(remaining)}: clean so far {len(clean)}", flush=True)

    clean_records = len({c["record"] for c in clean})
    print(f"\nnot yet texted {len(remaining)} | excluded {len(remaining) - len(clean)} | "
          f"CLEAN {len(clean)} phones on {clean_records} properties")
    for k, n in reasons.most_common():
        print(f"  {n:5}  {k}")
    if gate_would:
        print("the suppression gate would also exclude (report mode, nobody was excluded):")
        for k, n in gate_would.most_common():
            print(f"  {n:5}  {k}")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"remaining_{store.now()[:19].replace(':', '')}.json"
    path.write_text(json.dumps({"source": len(source), "texted": len(texted), "remaining": len(remaining),
                                "clean": clean, "excluded_by_first_reason": per_reason}, indent=1), encoding="utf-8")
    print("written", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
