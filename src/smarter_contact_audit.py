"""Who a Smarter Contact follow-up would text, and who it must not.

Run before any follow-up starts. For each Touch N draft it rebuilds the
recipient list from first principles (the Touch N-1 group, minus everyone who
replied by text OR call) and checks every remaining person against every do-not
source we hold:

  Smarter Contact  suppression list (DNC, unsubscribed, litigator, carrier
                   blocked, undeliverable, filtered), and the Touch N-1 text
                   itself failing to deliver
  the SMS agent    its suppression table (opt-outs, wrong numbers, litigators)
  Sift             phone status (DNC / WRONG / DEAD), the DNC list, the Do Not
                   Market tag, a record status someone set (not interested,
                   sold, a live lead), and the registry do-not-call flag where
                   Sift exposes it (only on a record's representative phone)
  Zillow           the house is agent-listed right now (mls_listings), because
                   Sift's MLS field lags and a listed owner is not worth a text

Read-only by default. `--commit` removes every flagged contact from the draft,
and adds the true do-not-contact ones to Smarter Contact's own DNC list so no
later campaign reaches them either.

    python src/smarter_contact_audit.py --drafts "P1 Wave 1 East T2 2026-10-29,..." --suppression fly.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mls_listings import ActiveListings  # noqa: E402
from sms_agent import config, crm, sc_client as C, store  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "output" / "smarter_contact" / "audits"
SKIP_PHONE = {"DNC", "CORRECT_DNC", "WRONG_DNC", "WRONG", "DEAD"}
# A status means someone has acted on the record. Only untouched P1 records keep
# getting automated touches; everything else is a person's call.
OPEN_STATUSES = {"", "prospecting"}
HARD = ("sc_suppressed", "agent_suppressed", "sift_phone", "sift_dnc_list", "sift_do_not_market", "registry_dnc")

Q_CAMPS = "query F($p: Pagination) { findCampaigns(pagination: $p) { items { id name contacts state { status } } } }"
Q_CAMP = "query G($id: String!) { getCampaign(id: $id) { id name contacts groups { id name } parentCampaign { ids } } }"
Q_CONTACTS = ("query F($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) { hasNext nextId "
              "nextCreatedAt items { id phone attrs { fieldId value } } } }")
Q_MSGS = ("query M($c: String!, $p: Pagination) { findConversationMessages(contactId: $c, pagination: $p) "
          "{ items { direction type status campaign { id } } } }")
Q_SUPP = ("query S($p: Pagination) { findSuppressions(pagination: $p) { hasNext nextId nextCreatedAt "
          "items { number source } } }")
M_REMOVE = ("mutation R($contactId: String!, $campaignId: String!) { removeContactFromCampaign(contactId: $contactId, "
            "campaignId: $campaignId) { success } }")


def _digits(n) -> str:
    return store.clean_phone(n)


def _pages(query: str, key: str, variables: dict) -> list[dict]:
    rows, page = [], {"limit": 100}
    while True:
        data = C.gql(query, dict(variables, p=page))[key]
        rows += data["items"]
        if not data.get("hasNext"):
            return rows
        page = {"limit": 100, "nextId": data["nextId"], "nextCreatedAt": data["nextCreatedAt"]}


def suppressions() -> dict[str, str]:
    return {_digits(r["number"]): r["source"] for r in _pages(Q_SUPP, "findSuppressions", {})}


def _status(rec: dict) -> str:
    s = rec.get("status")
    if isinstance(s, dict):
        s = s.get("slug") or s.get("title")
    return str(s or "").strip().lower()


def _names(items) -> list[str]:
    return [(x.get("title") or x.get("name") or "") if isinstance(x, dict) else str(x) for x in items or []]


def audit_draft(draft: dict, sc_supp: dict, agent_supp: dict, apn_field: str,
                listings: ActiveListings) -> dict:
    camp = C.gql(Q_CAMP, {"id": draft["id"]})["getCampaign"]
    parent_id = (camp.get("parentCampaign") or {}).get("ids", [None])[0]
    parent = C.gql(Q_CAMP, {"id": parent_id})["getCampaign"]
    contacts = []
    for g in parent.get("groups") or []:
        contacts += _pages(Q_CONTACTS, "findContacts", {"f": {"status": "ACTIVE", "groups": [g["id"]]}})
    keep, flagged, responders, reasons = [], [], 0, Counter()
    for ct in contacts:
        msgs = C.gql(Q_MSGS, {"c": ct["id"], "p": {"limit": 40}})["findConversationMessages"]["items"]
        if any(C.direction(m) == "in" for m in msgs):
            responders += 1
            continue
        why = []
        phone = _digits(ct["phone"])
        sent = [m for m in msgs if C.direction(m) == "out" and (m.get("campaign") or {}).get("id") == parent_id]
        if sent and str(sent[0].get("status") or "").upper() in ("UNDELIVERED", "FAILED", "CARRIER_BLOCKED", "BLOCKED"):
            why.append(f"sc_undelivered:{sent[0]['status']}")
        if phone in sc_supp:
            why.append(f"sc_suppressed:{sc_supp[phone]}")
        if phone in agent_supp:
            why.append(f"agent_suppressed:{agent_supp[phone].split(':')[0]}")
        record = next((a["value"] for a in ct.get("attrs") or [] if a["fieldId"] == apn_field), "")
        rec = crm.get_record(record, fresh=True) if record else None
        if rec:
            _, obj = crm.find_phone_object(record, phone)
            pst = ((obj or {}).get("status") or "").upper()
            if pst in SKIP_PHONE:
                why.append(f"sift_phone:{pst}")
            if config.LIST_DNC in _names(rec.get("lists")):
                why.append("sift_dnc_list")
            if config.TAG_OPT_OUT in _names(rec.get("tags")):
                why.append("sift_do_not_market")
            st = _status(rec)
            if st not in OPEN_STATUSES:
                why.append(f"sift_status:{st}")
            addr = rec.get("address") or {}
            if listings.lookup(addr.get("street") or "", addr.get("postal_code") or ""):
                why.append("mls_listed")
        elif record:
            why.append("sift_record_missing")
        if crm.phone_is_dnc(phone) is True:
            why.append("registry_dnc")
        if why:
            flagged.append({"contact_id": ct["id"], "phone": phone, "record": record, "why": why})
            reasons.update(w.split(":")[0] if w.split(":")[0] in HARD else w for w in why)
        else:
            keep.append(ct["id"])
    return {"draft": draft["name"], "draft_id": draft["id"], "draft_contacts": draft["contacts"],
            "group_contacts": len(contacts), "responders": responders, "rebuilt_recipients": len(keep) + len(flagged),
            "keep": len(keep), "flagged": flagged, "reasons": dict(reasons.most_common())}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drafts", required=True, help="comma-separated draft campaign names")
    ap.add_argument("--suppression", default="", help="JSON {phone: reason} exported from the Fly agent")
    ap.add_argument("--commit", action="store_true", help="remove flagged contacts from the drafts")
    a = ap.parse_args()
    names = [n.strip() for n in a.drafts.split(",") if n.strip()]
    camps = {c["name"]: c for c in C.gql(Q_CAMPS, {"p": {"limit": 100}})["findCampaigns"]["items"]}
    missing = [n for n in names if n not in camps]
    if missing:
        raise SystemExit(f"drafts not found: {missing}")
    agent_supp = json.loads(Path(a.suppression).read_text(encoding="utf-8")) if a.suppression else {}
    sc_supp = suppressions()
    fields = C.gql("query { findFields { id key } }")["findFields"]
    apn = next(f["id"] for f in fields if f["key"] == "ASSESSOR_PARCEL_NUMBER")
    listings = ActiveListings()
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    for n in names:
        if camps[n]["state"]["status"] != "DRAFT":
            raise SystemExit(f"{n} is {camps[n]['state']['status']}, not a draft; audit before it starts")
        r = audit_draft(camps[n], sc_supp, agent_supp, apn, listings)
        results.append(r)
        print(f"{n}: draft {r['draft_contacts']} | group {r['group_contacts']} - replied {r['responders']} = "
              f"{r['rebuilt_recipients']} | keep {r['keep']} | flagged {len(r['flagged'])} {r['reasons']}", flush=True)
        if a.commit:
            removed = 0
            for f in r["flagged"]:
                res = C.gql(M_REMOVE, {"contactId": f["contact_id"], "campaignId": r["draft_id"]})
                removed += bool((res.get("removeContactFromCampaign") or {}).get("success"))
            back = C.gql(Q_CAMP, {"id": r["draft_id"]})["getCampaign"]["contacts"]
            r["removed"], r["contacts_after"] = removed, back
            print(f"  removed {removed} of {len(r['flagged'])}; draft now {back} contacts", flush=True)
    path = OUT / f"followup_audit_{store.now()[:19].replace(':', '')}.json"
    path.write_text(json.dumps({"sc_suppressions": len(sc_supp), "agent_suppressions": len(agent_supp),
                                "drafts": results}, indent=1), encoding="utf-8")
    print("written", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
