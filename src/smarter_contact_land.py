#!/usr/bin/env python3
"""Land contacts in Smarter Contact: put the real parcel on every contact DataSift pushed, then split the group.

DataSift's Smarter Contact integration writes its RECORD UUID into the contact's APN field and leaves Parcel
acres and Parcel county empty (read off a live Priority 1 contact, 2026-11-10). The land templates print
<APN>, <Parcel acres> and <Parcel county>, so without this step a land owner would be texted a uuid. After a
land push this module moves the uuid into a "Sift Record" custom field (sc_client.contact_record_uuid reads
that first, so a reply still reaches the right record) and writes the parcel number, acres, county and state
from the land model (src/land_market.py).

    python src/smarter_contact_land.py --phase field                                  # dry; --commit creates it
    python src/smarter_contact_land.py --phase patch --group "Land Pilot Lots"        # dry: what would change
    python src/smarter_contact_land.py --phase patch --group "Land Pilot Lots" --commit          # ONE, read back
    python src/smarter_contact_land.py --phase patch --group "Land Pilot Lots" --commit --all    # the rest
    python src/smarter_contact_land.py --phase split --group "Land Pilot Lots"        # dry; --commit makes 4 groups
    python src/smarter_contact_land.py --phase qa --group "Land Pilot Lots"           # read-only counts

Rules:
  * Every write sends the contact's FULL attribute list with the changes applied, so a replace-style update
    cannot drop the phone, name or mailing address. The first write is still one contact read back in full:
    the new values must be there and every other field unchanged, or the run stops before a second contact.
  * A contact is matched to its land row through the pull_by_ids state (record uuid -> dataflik id), then the
    pilot rows by dataflik id. No match = left alone and counted, never guessed by address.
  * Property Address is written only when blank (41 of 8,157 mailable land rows have none).
  * split mirrors the Priority 1 launch rules: the no-name test is smarter_contact_templates.no_name_reason
    (Smarter Contact prints the raw first-name field), and the time zone is the phone's area code, because the
    delivery window runs on the account's Eastern clock.
  * Idempotent: a contact that already carries the right values is skipped; a group that exists is reported.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

PILOT = ROOT / "output" / "land_market" / "pilot"
PULL_STATE = ROOT / "output" / "pull_by_ids"
OPS = ROOT / "output" / "smarter_contact" / "discover" / "gql_ops.json"
COUNTY = {"47093": "Knox", "47009": "Blount", "47155": "Sevier", "47001": "Anderson", "47105": "Loudon",
          "47145": "Roane", "47089": "Jefferson", "47013": "Campbell", "47057": "Grainger", "47173": "Union",
          "47129": "Morgan"}
SIFT_KEY = "SIFT_RECORD"          # pseudo key for the custom field, which carries no predefined key
LAND_KEYS = ("ASSESSOR_PARCEL_NUMBER", "PARCEL_ACRES", "PARCEL_COUNTY", "PARCEL_STATE", SIFT_KEY)
KEEP_KEYS = ("PHONE", "FIRST_NAME", "LAST_NAME", "EMAIL", "MAILING_ADDRESS", "MAILING_CITY", "MAILING_STATE",
             "MAILING_ZIP", "CITY", "STATE", "ZIP")
Q_FIELDS = "query { findFields { id key name } }"
Q_CONTACTS = ("query P($filter: ContactFilter, $pagination: Pagination) { findContacts(filter: $filter, "
              "pagination: $pagination) { hasNext nextId nextCreatedAt items { id phone firstName lastName "
              "attrs { fieldId value } } } }")
Q_CONTACT = "query G($id: String!) { getContact(id: $id) { id attrs { fieldId value } } }"
M_UPDATE = ("mutation U($input: UpdateContactInput!, $id: String!) { updateContact(input: $input, id: $id) "
            "{ id } }")
M_FIELD = "mutation C($input: CreateFieldInput!) { createField(input: $input) { id key name } }"
M_TPL = ("mutation CreateMessageTemplate($input: CreateMessageTemplateInput!) { "
         "createMessageTemplate(input: $input) { id name template { content messageType optOut segments } } }")
M_TPL_UPDATE = ("mutation UpdateMessageTemplate($input: UpdateMessageTemplateInput!, $updateMessageTemplateId: "
                "String!) { updateMessageTemplate(input: $input, id: $updateMessageTemplateId) { id name template "
                "{ content messageType optOut segments } } }")
M_GROUP = "mutation CreateGroup($input: CreateGroupInput!) { createGroup(input: $input) { id name } }"
M_ADD = "mutation AddGroupToContacts($input: AddGroupToContactsInput!) { addGroupToContacts(input: $input) { success } }"
UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def is_uuid(v) -> bool:
    return bool(UUID_RX.match(str(v or "").strip()))


def parcel_display(pn) -> str:
    """The parcel number as an owner reads it on a tax bill: upper case, single spaces ('019kb024' -> '019KB024',
    '024  00501' -> '024 00501'); dashed counties keep their dashes."""
    return re.sub(r"\s+", " ", str(pn or "")).strip().upper()


def acres_display(a) -> str:
    """'42' for a tract, '6.4' for acreage, '0.62' for a lot; '' when unknown."""
    try:
        a = float(a or 0)
    except (TypeError, ValueError):
        return ""
    if a <= 0:
        return ""
    if a >= 10:
        return str(int(round(a)))
    return (("%.1f" if a >= 1 else "%.2f") % a).rstrip("0").rstrip(".")


def land_rows(paths=None) -> dict:
    rows = {}
    for p in paths or sorted(glob.glob(str(PILOT / "*.json"))):
        for r in json.loads(Path(p).read_text(encoding="utf-8")):
            rows[str(r["dataflik_id"])] = r
    return rows


def uuid_map(root: Path = PULL_STATE, pattern: str = "pilot_*/state.json") -> dict:
    """record uuid -> dataflik id, from the land pulls' pull_by_ids state (new_uuids and existing are id -> uuid).
    Only the land pull folders are read, so another account's pull can never be matched."""
    out = {}
    for p in glob.glob(str(root / pattern)):
        try:
            st = json.loads(Path(p).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(st, dict):
            continue
        for k in ("new_uuids", "existing"):
            for i, u in (st.get(k) or {}).items() if isinstance(st.get(k), dict) else ():
                if is_uuid(u):
                    out[u] = str(i)
    return out


def values_by_key(attrs: list, ids: dict) -> dict:
    """{field key: value} from a contact's attrs; ids is field id -> key (custom field mapped to SIFT_KEY)."""
    return {ids[a["fieldId"]]: (a.get("value") or "").strip() for a in attrs or [] if a.get("fieldId") in ids}


def record_uuid(values: dict) -> str:
    for k in (SIFT_KEY, "ASSESSOR_PARCEL_NUMBER"):
        if is_uuid(values.get(k)):
            return values[k]
    return ""


def wanted(values: dict, row: dict, uuid: str) -> dict:
    """{key: new value} for the fields that need writing (empty when the contact is already right)."""
    want = {"ASSESSOR_PARCEL_NUMBER": parcel_display(row.get("parcel_number")),
            "PARCEL_ACRES": acres_display(row.get("acres")),
            "PARCEL_COUNTY": COUNTY.get(str(row.get("fips")), ""),
            "PARCEL_STATE": "TN", SIFT_KEY: uuid}
    if not values.get("ADDRESS") and (row.get("property_address") or "").strip():
        want["ADDRESS"] = row["property_address"].strip()
    return {k: v for k, v in want.items() if v and values.get(k) != v}


def full_attrs(attrs: list, changes: dict, key_ids: dict) -> list:
    """The contact's whole attribute list with the changes applied, so no field can be dropped by the write."""
    by_id = {a["fieldId"]: (a.get("value") or "") for a in attrs or []}
    for k, v in changes.items():
        by_id[key_ids[k]] = v
    return [{"fieldId": f, "value": v} for f, v in by_id.items()]


def _sc():
    load_env()
    from sms_agent import sc_client
    return sc_client


def _fields(sc) -> tuple[dict, dict]:
    """(field id -> key, key -> field id), with the Sift Record custom field under SIFT_KEY."""
    ids = {}
    for f in sc.gql(Q_FIELDS).get("findFields") or []:
        if f.get("key"):
            ids[f["id"]] = f["key"]
        elif (f.get("name") or "").strip().casefold() == sc.SIFT_RECORD_FIELD.casefold():
            ids[f["id"]] = SIFT_KEY
    return ids, {k: i for i, k in ids.items()}


def _group(sc, name: str) -> dict:
    ops = json.loads(OPS.read_text(encoding="utf-8"))
    v = json.loads(json.dumps(ops["FindGroupsList"]["variables"] or {}))
    v.setdefault("pagination", {})["limit"] = 200
    d = sc.gql(ops["FindGroupsList"]["query"], v).get("findGroups") or {}
    hit = [g for g in d.get("items") or [] if g.get("name") == name]
    if not hit:
        raise SystemExit(f"no Smarter Contact group named {name!r}")
    return hit[0]


def _contacts(sc, gid: str) -> list:
    rows, pg = [], {"limit": 200}
    while True:
        d = sc.gql(Q_CONTACTS, {"filter": {"status": "ACTIVE", "groups": [gid]}, "pagination": pg}).get("findContacts")
        rows += d["items"]
        if not d["hasNext"]:
            return rows
        pg = {"limit": 200, "nextId": d["nextId"], "nextCreatedAt": d["nextCreatedAt"]}


def phase_field(args) -> int:
    sc = _sc()
    ids, keys = _fields(sc)
    if SIFT_KEY in keys:
        print(f"field {sc.SIFT_RECORD_FIELD!r} exists ({keys[SIFT_KEY]})")
        return 0
    if not args.commit:
        print(f"DRY RUN: would create the custom field {sc.SIFT_RECORD_FIELD!r}. Rerun with --commit.")
        return 0
    try:
        sc.gql(M_FIELD, {"input": {"name": sc.SIFT_RECORD_FIELD}})
    except Exception as e:  # noqa: BLE001  (the server's own message says which input is missing)
        print(f"createField refused: {str(e)[:400]}")
        return 2
    ids, keys = _fields(sc)
    print(f"created {sc.SIFT_RECORD_FIELD!r}: {'read back' if SIFT_KEY in keys else 'NOT FOUND on read-back'}")
    return 0 if SIFT_KEY in keys else 1


def phase_patch(args) -> int:
    sc = _sc()
    ids, keys = _fields(sc)
    if SIFT_KEY not in keys:
        raise SystemExit(f"run --phase field --commit first: no {sc.SIFT_RECORD_FIELD!r} field")
    group = _group(sc, args.group)
    contacts = _contacts(sc, group["id"])
    rows, umap = land_rows(args.rows), uuid_map()
    plan, why = [], {"no record uuid": 0, "record not in a land pull": 0, "already patched": 0}
    for c in contacts:
        vals = values_by_key(c.get("attrs"), ids)
        uuid = record_uuid(vals)
        if not uuid:
            why["no record uuid"] += 1
            continue
        row = rows.get(umap.get(uuid, ""))
        if not row:
            why["record not in a land pull"] += 1
            continue
        ch = wanted(vals, row, uuid)
        if not ch:
            why["already patched"] += 1
            continue
        plan.append((c, vals, ch))
    print(f"{args.group}: {len(contacts)} contacts, {len(plan)} to patch, " +
          ", ".join(f"{v} {k}" for k, v in why.items()))
    for c, vals, ch in plan[:5]:
        print("  %s -> %s" % (vals.get("ADDRESS") or ch.get("ADDRESS"),
                              {k: v for k, v in ch.items() if k != SIFT_KEY}))
    if not args.commit or not plan:
        if plan:
            print("DRY RUN: nothing written. Rerun with --commit (patches ONE contact and reads it back).")
        return 0
    todo = plan if args.all else plan[:1]
    for n, (c, vals, ch) in enumerate(todo, 1):
        sc.gql(M_UPDATE, {"input": {"attrs": full_attrs(c.get("attrs"), ch, keys)}, "id": c["id"]})
        if n == 1:
            back = values_by_key((sc.gql(Q_CONTACT, {"id": c["id"]}).get("getContact") or {}).get("attrs"), ids)
            bad = [k for k, v in ch.items() if back.get(k) != v] + \
                  [k for k in KEEP_KEYS + ("ADDRESS",) if k not in ch and back.get(k, "") != vals.get(k, "")]
            if bad:
                print(f"READ-BACK FAILED on contact {c['id']}: {bad}. Stopping; nothing else written.")
                return 1
            print(f"read back OK on contact {c['id']}: new values set, every other field unchanged")
        elif n % 100 == 0:
            print(f"  {n} / {len(todo)} patched")
        time.sleep(0.3)
    print(f"patched {len(todo)} of {len(plan)}" + ("" if args.all else "; rerun with --all for the rest"))
    return 0


def phase_split(args) -> int:
    """Four groups off the patched push: Named / No Name x East / Other TZ (the Priority 1 launch rules)."""
    import smarter_contact_templates as T
    from smarter_contact_launch import _is_eastern
    sc = _sc()
    group = _group(sc, args.group)
    contacts = _contacts(sc, group["id"])
    parts = {}
    for c in contacts:
        aud = "No Name" if T.no_name_reason({"first_name": c.get("firstName") or "",
                                             "last_name": c.get("lastName") or ""}) else "Named"
        tz = "East" if _is_eastern(c.get("phone")) else "Other TZ"
        parts.setdefault(f"{args.group} {aud} {tz}", []).append(c["id"])
    for name, cids in sorted(parts.items()):
        print(f"  {name}: {len(cids)}")
    if not args.commit:
        print("DRY RUN: no groups created. Rerun with --commit.")
        return 0
    for name, cids in sorted(parts.items()):
        try:
            _group(sc, name)
            print(f"  {name}: exists, left as is")
            continue
        except SystemExit:
            pass
        gid = sc.gql(M_GROUP, {"input": {"name": name}})["createGroup"]["id"]
        for i in range(0, len(cids), 200):
            sc.gql(M_ADD, {"input": {"groupId": gid, "ids": cids[i:i + 200]}})
        time.sleep(2)
        got = _group(sc, name).get("contacts")
        print(f"  {name}: created, {got} read back (plan {len(cids)})")
    return 0


def _templates(sc) -> dict:
    ops = json.loads(OPS.read_text(encoding="utf-8"))
    out, pg = {}, {"limit": 100}
    while True:
        d = sc.gql(ops["FindMessageTemplates"]["query"], {"pagination": pg}).get("findMessageTemplates") or {}
        for t in d.get("items") or []:
            out[t["name"].casefold()] = t
        if not d.get("hasNext"):
            return out
        pg = {"limit": 100, "nextId": d["nextId"], "nextCreatedAt": d["nextCreatedAt"]}


def phase_templates(args) -> int:
    """The land templates in Smarter Contact, over the agent's HTTP login (the browser build path's twin):
    created or updated in place by title, each read back as one segment with the module's opt-out setting."""
    import smarter_contact_templates as T
    found = T.land_audit()
    if found:
        raise SystemExit("land templates fail their checks: %s" % found[:3])
    sc = _sc()
    have = _templates(sc)
    todo = []
    for lane, n, named, noname in T.land_arms():
        for aud, pool in (("named", named), ("noname", noname)):
            for i, body in enumerate(pool, 1):
                title, content = T.land_title(lane, n, aud, i), T.to_sc(body, args.sender)
                cur = have.get(title.casefold())
                if cur and (cur.get("template") or {}).get("content") == content \
                        and (cur.get("template") or {}).get("optOut") == T.SC_OPT_OUT:
                    continue
                todo.append((title, content, n, cur))
    print(f"land templates: {len(todo)} to create or update, signed {args.sender!r}")
    if not args.commit:
        for t, c, *_ in todo[:3]:
            print(f"  {t}: {c}")
        print("DRY RUN: nothing written. Rerun with --commit.")
        return 0
    bad = []
    for title, content, n, cur in todo:
        tpl = {"content": content, "optOut": T.SC_OPT_OUT, "messageType": "INITIAL" if n == 1 else "FOLLOWUP"}
        try:
            if cur:
                r = sc.gql(M_TPL_UPDATE, {"input": {"name": title, "template": tpl},
                                          "updateMessageTemplateId": cur["id"]})["updateMessageTemplate"]
            else:
                r = sc.gql(M_TPL, {"input": {"name": title, "type": "SMS", "template": tpl}})["createMessageTemplate"]
        except Exception as e:  # noqa: BLE001  (a refused template is reported, the rest still go)
            bad.append(f"{title}: {str(e)[:120]}")
            continue
        got = r.get("template") or {}
        if got.get("content") != content or got.get("segments") != 1 or got.get("optOut") != T.SC_OPT_OUT:
            bad.append(f"{title}: segments={got.get('segments')} optOut={got.get('optOut')}")
        time.sleep(0.2)
    print(f"wrote {len(todo)} land templates, {len(bad)} failed read-back" + (": %s" % bad[:3] if bad else ""))
    return 1 if bad else 0


# ----------------------------------------------------------------- direct create (2026-11-10)
#
# DataSift's push created each probe group instantly but no contacts ever arrived (two probes, 30 minutes,
# none of the probe owner's 9 numbers anywhere in Smarter Contact). So land contacts are created here, over
# Smarter Contact's own CreateContact, with the parcel fields set at creation: no push, no patch step. The
# filters are the Priority 1 follow-up audit's (smarter_contact_audit.py), applied BEFORE anyone is texted.

STATE = ROOT / "output" / "land_market" / "sc_created.json"
PHONE_TYPES = {"MOBILE", "VOIP", "UNKNOWN"}             # never a landline on a texting campaign
PHONE_STATUS_OK = {"", "UNKNOWN", "NO_ANSWER", "CORRECT"}  # DNC, CORRECT_DNC, WRONG, WRONG_DNC, DEAD stay out
TIER_RANK = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3}
MAX_PHONES = 3                                         # Priority 1 averaged ~3 numbers an owner
LANE_LIST = {"lots": "Land - Builder Lots", "tracts": "Land - Entitlement Tracts"}
LANE_STATE = {"lots": "pilot_pilot_lots_knox", "tracts": "pilot_pilot_tracts_a"}
LANE_GROUP = {"lots": "Land Lots", "tracts": "Land Tracts"}
M_CONTACT = "mutation C($input: CreateContactInput!) { createContact(input: $input) { id phone } }"
Q_SUPP = ("query S($p: Pagination) { findSuppressions(pagination: $p) { hasNext nextId nextCreatedAt "
          "items { number source } } }")
Q_ALL = ("query P($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) { hasNext nextId "
         "nextCreatedAt items { phone } } }")


def digits10(n) -> str:
    d = re.sub(r"\D", "", str(n or ""))
    return d[-10:] if len(d) >= 10 else ""


def pick_phones(phones: list, blocked: set) -> list:
    """Up to MAX_PHONES textable numbers, best first: a Trestle Dial First/Second tag, then a number someone
    already answered or a dialer reached, then the rest. A Drop-tier number is never texted."""
    from sms_agent import gate_rules
    cands = []
    for p in phones or []:
        n = digits10(p.get("number"))
        if not n or n in blocked:
            continue
        # The one do-not-text rule (sms_agent/gate_rules.py, decision D32): a phone whose status says wrong,
        # dead or do-not-call, or that carries the do-not-call registry flag, is never texted. The seeder and
        # every loader ask the same function. The registry numbers collected off the search rows arrive in
        # `blocked`, as they always have.
        if gate_rules.do_not_text(p):
            continue
        if (p.get("type") or "UNKNOWN").upper() not in PHONE_TYPES:
            continue
        if (p.get("status") or "").upper() not in PHONE_STATUS_OK:
            continue
        tags = p.get("tags") or []
        if "Drop" in tags:
            continue
        rank = min([TIER_RANK[t] for t in tags if t in TIER_RANK] or [4])
        cands.append((rank, 0 if (p.get("status") or "").upper() in ("CORRECT", "NO_ANSWER") else 1, n))
    out = []
    for _, _, n in sorted(cands):
        if n not in out:
            out.append(n)
    return out[:MAX_PHONES]


def record_skip(rec: dict) -> str:
    """Why this record's owner is not texted at all ('' when they are)."""
    from sms_agent import config
    o = rec.get("owner") or {}
    if o.get("deceased"):
        return "owner deceased"
    if o.get("dnc") or o.get("opt_out"):
        return "owner DNC or opted out"
    names = lambda xs: [(x.get("title") or x.get("name") or "") if isinstance(x, dict) else str(x) for x in xs or []]  # noqa: E731
    if config.LIST_DNC in names(rec.get("lists")):
        return "on the DNC list"
    if config.TAG_OPT_OUT in names(rec.get("tags")):
        return "tagged Do Not Market"
    st = rec.get("status")
    st = (st.get("slug") or st.get("title")) if isinstance(st, dict) else st
    if str(st or "").strip().lower() not in ("", "prospecting", "new", "none"):
        return f"status {st}"
    if config.GATE_MODE == "enforce":
        # The suppression gate (build plan 0.1), behind SMS_AGENT_GATE. It adds what the four checks above do
        # not read: a death recorded as a tag or on the Obituary list, a litigator, a sold tag, a listed
        # house, Mail Only, Not Single Family. With the switch off this function is exactly what it was.
        return gate_note(rec)
    return ""


def gate_note(rec: dict) -> str:
    """What the suppression gate says about this record ('' when it may be texted, or the gate is off).
    In `report` mode the caller counts these without holding anyone, so the effect can be read first."""
    from sms_agent import config
    if config.GATE_MODE == "off":
        return ""
    from sms_agent import gatekeeper
    found = gatekeeper.reasons(rec, program="seller")
    return found[0] if found else ""


def contact_input(rec: dict, row: dict, phone: str, keys: dict) -> dict:
    o = rec.get("owner") or {}
    a, m = rec.get("address") or {}, o.get("address") or {}
    company = (o.get("company") or "").strip()
    first = "" if company else (o.get("first_name") or "").strip()
    last = company or (o.get("last_name") or "").strip()
    vals = {"ADDRESS": (a.get("street") or row.get("property_address") or "").strip(),
            "CITY": a.get("city") or row.get("property_city") or "", "STATE": a.get("state") or "TN",
            "ZIP": str(a.get("postal_code") or row.get("property_zip") or "")[:5],
            "MAILING_ADDRESS": m.get("street") or "", "MAILING_CITY": m.get("city") or "",
            "MAILING_STATE": m.get("state") or "", "MAILING_ZIP": str(m.get("postal_code") or "")[:5],
            "ASSESSOR_PARCEL_NUMBER": parcel_display(row.get("parcel_number")),
            "PARCEL_ACRES": acres_display(row.get("acres")), "PARCEL_COUNTY": COUNTY.get(str(row.get("fips")), ""),
            "PARCEL_STATE": "TN", SIFT_KEY: rec.get("uuid") or ""}
    return {"phone": "+1" + phone, "firstName": first, "lastName": last,
            "attrs": [{"fieldId": keys[k], "value": v} for k, v in vals.items() if v and k in keys]}


def _read(sc, query: str, variables: dict, tries: int = 4) -> dict:
    """A READ retried on a network reset (WinError 10054 killed a dry run on page 1, 2026-11-10).
    Only reads come through here: a retried write could create a contact twice."""
    for n in range(tries):
        try:
            return sc.gql(query, variables)
        except Exception as e:  # noqa: BLE001 - SCError carries both network and HTTP failures
            if "network error" not in str(e) or n == tries - 1:
                raise
            time.sleep(5 * (n + 1))
    raise AssertionError("unreachable")


def _sc_pages(sc, query: str, key: str, variables: dict) -> list:
    rows, pg = [], {"limit": 200}
    while True:
        d = _read(sc, query, dict(variables, p=pg))[key]
        rows += d.get("items") or []
        if not d.get("hasNext"):
            return rows
        pg = {"limit": 200, "nextId": d["nextId"], "nextCreatedAt": d["nextCreatedAt"]}


def phase_create(args) -> int:
    import smarter_contact_templates as T
    from smarter_contact_launch import _is_eastern
    import pull_by_ids as P
    from datasift_api_upload import Api
    sc, api = _sc(), Api()
    ids, keys = _fields(sc)
    if SIFT_KEY not in keys:
        raise SystemExit("run --phase field --commit first")
    rows = land_rows()
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"contacts": {}}
    from sms_agent import config as agent_config, gate_rules
    blocked = {digits10(r["number"]) for r in _sc_pages(sc, Q_SUPP, "findSuppressions", {})}
    existing = {digits10(r["phone"]) for r in _sc_pages(sc, Q_ALL, "findContacts", {"f": {"status": "ACTIVE"}})}
    print(f"Smarter Contact: {len(existing)} contacts already there (never re-texted from here), "
          f"{len(blocked)} suppressed numbers")
    plan, why = [], collections.Counter()
    gate_would = collections.Counter()      # report mode: what the gate would hold, counted, not held
    all_dnc = set()                         # every registry-flagged number seen across the lanes
    for lane in args.lanes.split(","):
        st = json.loads((PULL_STATE / LANE_STATE[lane] / "state.json").read_text(encoding="utf-8"))
        uuids = dict(st.get("new_uuids") or {}, **(st.get("existing") or {}))
        lu = P.list_uuid(api, LANE_LIST[lane])
        dnc, off = set(), 0
        while True:            # the registry do-not-call flag lives only on the search row's phone
            r = api.call("/api/internal/property/", "POST", {"limit": 100, "offset": off,
                         "query": {"must": {"any_lists": [lu]}}}, headers={"x-http-method-override": "GET"})
            got = r.get("results") or []
            dnc |= gate_rules.flagged_numbers(got)      # the one registry rule, shared with the seeder
            off += len(got)
            if not got or off >= r.get("count", 0):
                break
        all_dnc |= dnc
        todo = list(uuids.items())[: args.limit or None]
        for n, (did, uuid) in enumerate(todo, 1):
            row = rows.get(str(did))
            if not row:
                why["not in the land model"] += 1
                continue
            try:
                rec = api.call(f"/api/internal/property/{uuid}/")
            except Exception:  # noqa: BLE001
                why["record unreadable"] += 1
                continue
            skip = record_skip(rec)
            if skip:
                why[skip] += 1
                continue
            if agent_config.GATE_MODE == "report":
                note = gate_note(rec)
                if note:
                    gate_would[note] += 1
            phones = pick_phones((rec.get("owner") or {}).get("phones"), blocked | existing | dnc)
            if not phones:
                why["no textable number"] += 1
                continue
            o = rec.get("owner") or {}
            aud = "No Name" if (o.get("company") or T.no_name_reason(
                {"first_name": o.get("first_name") or "", "last_name": o.get("last_name") or ""})) else "Named"
            for ph in phones:
                g = f"{LANE_GROUP[lane]} {aud} {'East' if _is_eastern(ph) else 'Other TZ'}"
                # A late pass gets its own groups: the base groups sit under campaigns already sending.
                g = f"{g} {args.group_suffix}".rstrip()
                plan.append({"lane": lane, "group": g,
                             "phone": ph, "input": contact_input(rec, row, ph, keys), "record": uuid})
            if n % 200 == 0:
                print(f"  {lane}: {n} / {len(todo)} records read", flush=True)
            time.sleep(0.15)
    seen, uniq = set(), []
    for x in plan:                          # one phone is one contact, even if two parcels share an owner
        if x["phone"] not in seen:
            seen.add(x["phone"])
            uniq.append(x)
    groups = collections.Counter(x["group"] for x in uniq)
    print(f"plan: {len(uniq)} contacts from {len({x['record'] for x in uniq})} records; skipped " +
          ", ".join(f"{v} {k}" for k, v in why.most_common()))
    if gate_would:
        print("the suppression gate would also hold (report mode, nobody was held): " +
              ", ".join(f"{v} {k}" for k, v in gate_would.most_common()))
    if agent_config.GATE_MODE != "off" and all_dnc:
        from sms_agent import gatekeeper
        gatekeeper.remember_flagged(all_dnc)    # so the same number is refused wherever it turns up next
    for g, v in sorted(groups.items()):
        print(f"  {g}: {v}")
    if not args.commit:
        print("DRY RUN: nothing created. Rerun with --commit.")
        return 0
    made = state["contacts"]
    for i, x in enumerate(uniq, 1):
        if x["phone"] in made:
            continue
        try:
            c = sc.gql(M_CONTACT, {"input": x["input"]})["createContact"]
            made[x["phone"]] = {"id": c["id"], "group": x["group"], "record": x["record"]}
        except Exception as e:  # noqa: BLE001
            why[f"create refused: {str(e)[:60]}"] += 1
        if i % 50 == 0:
            STATE.write_text(json.dumps(state), encoding="utf-8")
            print(f"  {i} / {len(uniq)} created", flush=True)
        time.sleep(0.2)
    STATE.write_text(json.dumps(state), encoding="utf-8")
    for g in sorted(groups):
        cids = [v["id"] for v in made.values() if v["group"] == g]
        try:
            gid = _group(sc, g)["id"]
        except SystemExit:
            gid = sc.gql(M_GROUP, {"input": {"name": g}})["createGroup"]["id"]
        for k in range(0, len(cids), 200):
            sc.gql(M_ADD, {"input": {"groupId": gid, "ids": cids[k:k + 200]}})
        time.sleep(2)
        print(f"  group {g}: {_group(sc, g).get('contacts')} contacts read back (plan {len(cids)})")
    refused = {k: v for k, v in why.items() if k.startswith("create refused")}
    if refused:
        print("refused:", refused)
    return 0


def phase_group(args) -> int:
    """Put every land contact in its group, reading Smarter Contact itself rather than the state file: a contact
    created after the last state save (a killed run, 2026-11-10) still carries its Sift Record, so it is found.
    Group = lane (the record's land row) x Named/No Name (blank first name = no-name) x East/Other TZ."""
    import smarter_contact_templates as T
    from smarter_contact_launch import _is_eastern
    sc = _sc()
    ids, keys = _fields(sc)
    rows, umap = land_rows(), uuid_map()
    q = ("query P($f: ContactFilter, $p: Pagination) { findContacts(filter: $f, pagination: $p) { hasNext nextId "
         "nextCreatedAt items { id phone firstName lastName attrs { fieldId value } } } }")
    want = collections.defaultdict(list)
    for c in _sc_pages(sc, q, "findContacts", {"f": {"status": "ACTIVE"}}):
        vals = values_by_key(c.get("attrs"), ids)
        uuid = vals.get(SIFT_KEY) or ""
        row = rows.get(umap.get(uuid, "")) if is_uuid(uuid) else None
        if not row:
            continue
        lane = "lots" if row.get("lane") in ("lot", "homesite") else "tracts"
        first, last = (c.get("firstName") or "").strip(), (c.get("lastName") or "").strip()
        # the build's own rule: a company (blank first name) or a first name Smarter Contact would print badly
        aud = "No Name" if (not first or T.no_name_reason({"first_name": first, "last_name": last})) else "Named"
        tz = "East" if _is_eastern(digits10(c.get("phone"))) else "Other TZ"
        want[f"{LANE_GROUP[lane]} {aud} {tz}"].append(c["id"])
    for g, cids in sorted(want.items()):
        try:
            grp = _group(sc, g)
        except SystemExit:
            grp = {"id": sc.gql(M_GROUP, {"input": {"name": g}})["createGroup"]["id"], "contacts": 0} \
                if args.commit else {"id": "", "contacts": 0}
        print(f"  {g}: {len(cids)} land contacts, group holds {grp.get('contacts')}")
        if args.commit:
            for k in range(0, len(cids), 200):
                sc.gql(M_ADD, {"input": {"groupId": grp["id"], "ids": cids[k:k + 200]}})
    if args.commit:
        time.sleep(3)
        for g in sorted(want):
            print(f"  read back {g}: {_group(sc, g).get('contacts')}")
    else:
        print("DRY RUN: no groups changed. Rerun with --commit.")
    return 0


# ---------------------------------------------------------------------------
# Deal card (Ty, 2026-11-10): the land model's numbers where a reply is worked, so whoever answers
# knows the offer range and what the parcel could sell for. Written to a pinned note on the Sift
# record and to three Smarter Contact fields. NEVER in a text and never in the agent's prompt: the
# agent reads a contact's attributes only for its record uuid (sc_client.contact_record_uuid).
SCORED = ROOT / "output" / "land_market" / "scored.jsonl"
DEALS_STATE = ROOT / "output" / "land_market" / "deal_cards.json"
DEAL_MARKER = "Land deal card"
DEAL_FIELDS = {"LAND_OFFER": "Land Offer", "LAND_VALUE": "Land Value", "LAND_NOTES": "Land Notes"}
LANE_LABEL = {"lot": "buildable lot", "homesite": "homesite", "small_tract": "small tract",
              "ent_tract": "entitlement tract", "large_tract": "large tract"}


def money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "unknown"


def scored_rows(ids: set, path: Path = SCORED) -> dict:
    """Full scored rows (the pilot files carry only the buy side) for these dataflik ids."""
    out = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if str(r.get("dataflik_id")) in ids:
                out[str(r["dataflik_id"])] = r
    return out


def _watch(r: dict) -> list[str]:
    """The site facts that move a price or kill a deal, worst first."""
    w = []
    if r.get("landlocked"):
        w.append("landlocked")
    if (r.get("is_active_listing")):
        w.append("listed on the MLS")
    for key, label in (("floodway_pct", "floodway"), ("flood_pct", "flood zone")):
        if (r.get(key) or 0) >= 0.05:
            w.append(f"{round(r[key] * 100)}% {label}")
            break
    if (r.get("steep_share") or 0) >= 0.25:
        w.append(f"{round(r['steep_share'] * 100)}% steep (avg grade {round(r.get('slope_mean_pct') or 0)}%)")
    if (r.get("sits_low_ft") or 0) >= 2:
        w.append(f"sits {round(r['sits_low_ft'])} ft low")
    if r.get("sewer"):
        w.append(str(r["sewer"]))
    fr = r.get("frontage_ft")
    if fr is not None and not r.get("landlocked") and fr < 50:
        w.append(f"only {round(fr)} ft of frontage")
    if r.get("zoning"):
        w.append(f"zoned {r['zoning']}")
    return w


LAND_PARCELS = ROOT / "output" / "land_market" / "land_parcels.jsonl"
BUILDER_ANALYSIS = ROOT / "output" / "builder_land" / "knoxville_region" / "analysis.json"
NEWBUILD = ROOT / "output" / "land_market" / "newbuild_pts.json"
FITS = ROOT / "output" / "land_market" / "fits.json"
# How far a buyer of similar land counts as a buyer for this parcel: a lot competes on its street, a tract
# across a county. Size band: 0.4x to 2.5x the parcel's acres (a 0.3 ac lot and a 60 ac tract are not
# the same buyer).
BUY_RADIUS_MI = {"lot": 1.0, "homesite": 3.0, "small_tract": 5.0, "ent_tract": 10.0, "large_tract": 10.0}
BUY_YEARS, BUY_BAND, NEWBUILD_MI, NEWBUILD_SINCE = 3, (0.4, 2.5), 2.0, 2023
_ORG = re.compile(r"\b(LLC|INC|CORP|CO|COMPANY|LP|LLP|LTD|HOMES|BUILDERS|CONSTRUCTION|PROPERTIES|INVESTMENTS|"
                  r"HOLDINGS|DEVELOPMENT|GROUP|PARTNERS|TRUST|BANK|CHURCH)\b")


def _miles(lat1, lon1, lat2, lon2) -> float:
    import math
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 7917.5 * math.asin(math.sqrt(a))


def _name_key(n: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", str(n or "").upper()).split())


class BuyerIndex:
    """Who buys land like this, near this parcel. Three reads, each from data we already pulled:
    * builders who fit it: the deed-read buy boxes (builder_land), with each builder's pace and prices;
    * land buyers nearby: the current owner of a land parcel whose last sale was arm's length in the last
      3 years IS that parcel's buyer (scored.jsonl, 141,543 verified parcels). A parcel the model marks
      BUILT (a house went up after the county last classed it vacant) is a HOUSE sale to a homebuyer, not a
      land buyer: 3 of the 6 "buyers" near one Knox lot were exactly that, at $237,000-$319,900;
    * new homes nearby since 2023: demand on the ground (newbuild_pts.json).
    A builder who built on a lot leaves the land file, so land buyers undercount builders; the first read
    covers them."""

    def __init__(self, asof: float | None = None):
        asof = asof or time.time()
        cutoff = (asof - BUY_YEARS * 365.25 * 86400) * 1000
        self.grid = collections.defaultdict(list)
        self.repeat = collections.Counter()
        with SCORED.open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if (r.get("last_sale_date") or 0) < cutoff or r.get("is_last_sale_intrafamily"):
                    continue
                if "BUILT" in str(r.get("verdict") or "") or r.get("with_house") or r.get("status") in ("house", "mobile"):
                    continue
                if (r.get("last_sale_price") or 0) < 1000 or r.get("lat") is None or not r.get("full_name"):
                    continue
                key = _name_key(r["full_name"])
                corp = bool(r.get("owner_cat") == "entity" or r.get("is_corporate_owned") or _ORG.search(key))
                rec = (r["lat"], r["lon"], r.get("lane"), r.get("acres") or r.get("area_lot_acres") or 0, key,
                       r["last_sale_price"], corp)
                self.grid[(round(r["lat"] * 10), round(r["lon"] * 10))].append(rec)
                self.repeat[key] += 1
        a = json.loads(BUILDER_ANALYSIS.read_text(encoding="utf-8")) if BUILDER_ANALYSIS.exists() else {}
        self.boxes = {b["key"]: b for b in a.get("boxes") or []}
        self.matches = {str(x["id"]): x.get("matches") or [] for x in (a.get("lots") or []) + (a.get("tracts") or [])}
        self.fits = json.loads(FITS.read_text(encoding="utf-8")) if FITS.exists() else {}
        self.newbuild = json.loads(NEWBUILD.read_text(encoding="utf-8")) if NEWBUILD.exists() else []

    def _near(self, lat, lon, miles):
        span = int(miles / 6.9) + 1          # 0.1 degree of latitude is about 6.9 miles
        cy, cx = round(lat * 10), round(lon * 10)
        for dy in range(-span, span + 1):
            for dx in range(-span - 1, span + 2):
                for rec in self.grid.get((cy + dy, cx + dx), ()):
                    if _miles(lat, lon, rec[0], rec[1]) <= miles:
                        yield rec

    def summary(self, r: dict) -> dict:
        lane, acres = r.get("lane") or "", r.get("acres") or 0
        lat, lon = r.get("lat"), r.get("lon")
        out = {"radius": BUY_RADIUS_MI.get(lane, 3.0), "builders": [], "n_fit": 0}
        tractish = lane.endswith("tract")
        if lat is not None and acres:
            lo, hi = acres * BUY_BAND[0], acres * BUY_BAND[1]
            own = _name_key(r.get("full_name"))
            sales = [s for s in self._near(lat, lon, out["radius"])
                     if lo <= (s[3] or 0) <= hi and s[4] != own and (str(s[2]).endswith("tract") == tractish)]
            names = {s[4] for s in sales}
            out.update(land_buyers=len(names), companies=len({s[4] for s in sales if s[6]}),
                       repeat=len({n for n in names if self.repeat[n] >= 2}), sales=len(sales))
            if sales:
                vals = sorted((s[5] / s[3]) if tractish and s[3] else s[5] for s in sales)
                out["median_paid"] = vals[len(vals) // 2]
            homes = [p for p in self.newbuild if p[3] and p[3] >= NEWBUILD_SINCE
                     and _miles(lat, lon, p[1], p[2]) <= NEWBUILD_MI]
            if homes:
                v = sorted(p[4] for p in homes if p[4])
                out.update(new_homes=len(homes), new_home_med=v[len(v) // 2] if v else None)
        ms = self.matches.get(str(r.get("dataflik_id"))) or []
        if ms:
            out["n_fit"] = len(ms)
            for m in sorted(ms, key=lambda m: -(m.get("score") or 0))[:3]:
                b = self.boxes.get(m.get("key")) or {}
                cls = (b.get("classes") or {})
                price = next((c.get("price_med") for c in cls.values() if c.get("price_med")), None)
                out["builders"].append({"name": _biz(m.get("name") or m.get("key")), "miles": (m.get("dist") or 0) / 1609,
                                        "nearby": m.get("nearby"), "buys_12m": b.get("buys_12m"), "price": price,
                                        "sells_for": b.get("sells_for"), "reach": b.get("reach")})
        else:
            f = self.fits.get(str(r.get("dataflik_id"))) or {}
            out["n_fit"] = f.get("n_fit") or len(r.get("builders_fit") or [])
            out["builders"] = [{"name": _biz(n)} for n in (f.get("builders") or r.get("builders_fit") or [])[:3]]
        return out


def buyer_lines(r: dict, b: dict) -> tuple[list[str], str]:
    """The buyers block of a deal card, and its one-line form for Smarter Contact."""
    lines, tract = [], (r.get("lane") or "").endswith("tract")
    if b.get("n_fit"):
        parts = []
        for x in b["builders"]:
            bits = [x["name"]]
            if x.get("miles") is not None:
                bits.append(f"{x['miles']:.1f} mi from its nearest buy")
            if x.get("buys_12m"):
                bits.append(f"{x['buys_12m']} buys in the last 12 months")
            if x.get("price"):
                bits.append(f"pays about {money(x['price'])}")
            if x.get("sells_for"):
                bits.append(f"sells its homes for about {money(x['sells_for'])}")
            if x.get("reach"):
                bits.append(x["reach"])
            parts.append(f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0])
        lines.append(f"Builders who fit: {b['n_fit']}. Top: " + "; ".join(parts) + ".")
    else:
        lines.append("Builders who fit: none matched yet." + (" Line up a builder before spending on engineering."
                                                              if tract else ""))
    if b.get("land_buyers") is not None:
        paid = ""
        if b.get("median_paid"):
            paid = f", median paid {money(b['median_paid'])}" + (" an acre" if tract else "")
        lines.append(f"Land buyers nearby: {b['land_buyers']} different buyers bought similar land within "
                     f"{b['radius']:g} mi in the last {BUY_YEARS} years ({b['companies']} companies, {b['repeat']} "
                     f"who bought more than once){paid}.")
    if b.get("new_homes"):
        lines.append(f"New homes: {b['new_homes']} built within {NEWBUILD_MI:g} mi since {NEWBUILD_SINCE}"
                     + (f", median value {money(b['new_home_med'])}." if b.get("new_home_med") else "."))
    short = f"{b.get('n_fit') or 0} builders fit, {b.get('land_buyers', 0)} land buyers within {b['radius']:g} mi"
    return lines, short


def _biz(name: str) -> str:
    """Title case a company name without turning LLC into Llc."""
    up = {"LLC", "LP", "LLP", "PLLC", "INC", "II", "III", "IV", "USA", "TN"}
    return " ".join(w if w.upper() in up and w.upper() != "INC" else ("Inc" if w.upper() == "INC" else w.title())
                    for w in str(name).split()).replace("Llc", "LLC")


HOW_WE_MAKE_MONEY = {
    "lot": "Builder lot desk. Sign it inside the offer range, assign the contract to a builder who fits, and "
           "keep the spread as our fee. No builder ready, no contract.",
    "tract": "Entitlement. Option it at farm price with earnest money, get a builder's letter of intent, get "
             "the lots approved by the county, then sell the approved land to that builder. No letter of "
             "intent, no engineering money.",
    "split": "Homesite split. Buy at farm price, survey and plat it into homesites, sell the homesites.",
}


def deal_card(r: dict, buyers: dict | None = None) -> dict:
    """{note, offer, value, notes} in plain words. A lot sells to a builder at what fitting builders paid
    nearby; a tract sells entitled at paper-lot value, so its spread is approved value minus the top of
    the offer range and the entitlement cost (est_spread in land_market.py). `buyers` (BuyerIndex.summary)
    adds who buys land like this near the parcel."""
    lane = r.get("lane") or ""
    tract = lane.endswith("tract")
    county = COUNTY.get(str(r.get("fips")), "")
    offer = f"{money(r.get('offer_lo'))}-{money(r.get('offer_hi'))}"
    model = "split" if "SPLIT" in str(r.get("verdict") or "") else ("tract" if tract else "lot")
    lines = [f"{DEAL_MARKER} (land model 2026-11-09, a starting point: verify before quoting any number)",
             f"{acres_display(r.get('acres'))} ac {LANE_LABEL.get(lane, lane)}, {county} County, parcel "
             f"{parcel_display(r.get('parcel_number'))}, {r.get('property_address') or 'no street address'}. "
             f"{r.get('verdict')}, tier {r.get('tier')}.",
             f"How we make money: {HOW_WE_MAKE_MONEY[model]}"]
    if tract:
        lots, spread = r.get("est_lots"), r.get("est_spread")
        lines += [f"Offer range: {offer}. Basis: farm price {money(r.get('farm_per_acre'))}/ac from "
                  f"{r.get('ref_basis')}.",
                  f"Could sell for: {money(r.get('approved_value'))} once approved: about {lots} lots ({r.get('path')}) "
                  f"on {r.get('dev_acres')} developable acres at {money(r.get('paper_lot_value'))} a paper lot, "
                  f"which is {r.get('paper_lot_basis')}.",
                  f"Entitlement cost about {money(r.get('entitle_cost'))}. Spread about {money(spread)} buying at "
                  f"the top of the range."]
        value = f"{money(r.get('approved_value'))} approved ({lots} lots), spread {money(spread)}"
    else:
        ref = r.get("ref_price")
        spread = (ref or 0) - (r.get("offer_hi") or 0)
        lines += [f"Offer range: {offer}.",
                  f"Could sell for: {money(ref)} to a builder. Basis: {r.get('ref_basis')}.",
                  f"Spread at the top of the range: {money(spread)}."]
        value = f"{money(ref)} to a builder, spread {money(spread)}"
    fit = [_biz(b) for b in r.get("builders_fit") or []]
    short = ""
    if buyers:
        blines, short = buyer_lines(r, buyers)
        lines += blines
    else:
        lines.append("Builders who fit: " + (", ".join(fit[:5]) if fit else
                                             "none matched yet. Line up a builder before spending on engineering."
                                             if tract else "none matched."))
    watch = _watch(r)
    if watch:
        lines.append("Watch: " + ", ".join(watch) + ".")
    own = [f"{r.get('segment')}" if r.get("segment") else ""]
    if r.get("tenure") and "held" not in str(r.get("segment") or "").lower():
        own.append(f"held {round(r['tenure'])} years")
    if r.get("last_sale_price"):
        yr = time.gmtime((r.get("last_sale_date") or 0) / 1000).tm_year if r.get("last_sale_date") else ""
        own.append(f"last sold {money(r['last_sale_price'])}" + (f" in {yr}" if yr else ""))
    if r.get("mailing_address_city"):
        own.append(f"mails from {r['mailing_address_city']} {r.get('mailing_address_state') or ''}".strip())
    own = [o for o in own if o]
    if own:
        lines.append("Owner: " + ", ".join(own) + ".")
    notes = "; ".join(([f"{r.get('est_lots')} lots, {r.get('path')}"] if tract else []) +
                      ([short] if short else [f"{len(fit)} builder{'s' if len(fit) != 1 else ''} fit"] if fit else [])
                      + watch[:3])
    return {"note": "\n".join(lines), "offer": offer, "value": value, "notes": notes[:250]}


def phase_deals(args) -> int:
    """The deal card on every land record (pinned Sift note, edited in place on a re-run) and on every
    land contact (Land Offer / Land Value / Land Notes). Dry by default; the first record is read back
    before the batch."""
    from datasift_api_upload import Api
    umap = uuid_map()
    rows = scored_rows({str(d) for d in umap.values()})
    idx = BuyerIndex()
    cards = {u: deal_card(rows[str(d)], idx.summary(rows[str(d)])) for u, d in umap.items() if str(d) in rows}
    missing = len(umap) - len(cards)
    print(f"{len(cards)} land records with a deal card ({missing} with no scored row)")
    first = (args.first, cards[args.first]) if args.first in cards else next(iter(cards.items()), None)
    if first:
        print("--- sample ---\n" + first[1]["note"] + f"\nSC: {first[1]['offer']} | {first[1]['value']} | "
              f"{first[1]['notes']}\n---")
    dashes = [u for u, c in cards.items() if any(ch in c["note"] + c["notes"] for ch in "—–")]
    if dashes:
        raise SystemExit(f"{len(dashes)} cards carry an em or en dash")
    if not args.commit:
        print("DRY RUN: nothing written. Rerun with --commit.")
        return 0
    state = json.loads(DEALS_STATE.read_text(encoding="utf-8")) if DEALS_STATE.exists() else {"notes": {}, "sc": []}
    api = Api()
    order = sorted(cards, key=lambda u: u != args.first) if args.first else list(cards)
    done = 0
    for i, uuid in enumerate(order, 1):
        note = cards[uuid]["note"]
        try:
            mu = state["notes"].get(uuid)
            if mu:
                api.call(f"/api/internal/property/{uuid}/message/{mu}/", "PATCH", {"message": note})
            else:
                mu = (api.call(f"/api/internal/property/{uuid}/message/", "POST", {"message": note}) or {}).get("uuid")
                if mu:
                    api.call(f"/api/internal/property/{uuid}/message/{mu}/pin/", "POST")
                    state["notes"][uuid] = mu
            done += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {uuid[:8]}: note failed {str(e)[:120]}")
        if i == 1:
            msgs = api.call(f"/api/internal/property/{uuid}/message/?limit=50")
            msgs = msgs.get("results") if isinstance(msgs, dict) else msgs
            got = next((m for m in msgs or [] if DEAL_MARKER in str(m.get("message") or "")), None)
            if not got or not got.get("pinned"):
                raise SystemExit(f"read-back failed on {uuid}: note {'missing' if not got else 'not pinned'}")
            print(f"  read back {uuid[:8]}: pinned note present")
        if i % 50 == 0:
            DEALS_STATE.write_text(json.dumps(state), encoding="utf-8")
            print(f"  {i} / {len(order)} notes", flush=True)
        time.sleep(0.3)
    DEALS_STATE.write_text(json.dumps(state), encoding="utf-8")
    print(f"Sift: {done} of {len(order)} deal notes written and pinned")
    # Smarter Contact: three fields on every land contact.
    sc = _sc()
    ids, keys = _fields(sc)
    by_name = {(f.get("name") or "").strip().casefold(): f["id"] for f in sc.gql(Q_FIELDS).get("findFields") or []}
    fid = {}
    for k, name in DEAL_FIELDS.items():
        fid[k] = by_name.get(name.casefold()) or sc.gql(M_FIELD, {"input": {"name": name}})["createField"]["id"]
    contacts = json.loads(STATE.read_text(encoding="utf-8"))["contacts"]
    seen = set(state.get("sc") or [])
    n = 0
    for phone, v in contacts.items():
        card = cards.get(v["record"])
        if not card or (v["id"] in seen and not args.refresh):
            continue
        try:
            c = _read(sc, Q_CONTACT, {"id": v["id"]})["getContact"]
            by_id = {a["fieldId"]: (a.get("value") or "") for a in c.get("attrs") or []}
            by_id.update({fid["LAND_OFFER"]: card["offer"], fid["LAND_VALUE"]: card["value"],
                          fid["LAND_NOTES"]: card["notes"]})
            sc.gql(M_UPDATE, {"input": {"attrs": [{"fieldId": f, "value": x} for f, x in by_id.items()]},
                              "id": v["id"]})
            seen.add(v["id"])
            n += 1
            if n == 1:
                back = {a["fieldId"]: a.get("value") for a in
                        _read(sc, Q_CONTACT, {"id": v["id"]})["getContact"].get("attrs") or []}
                lost = [f for f in by_id if f not in back]
                if back.get(fid["LAND_OFFER"]) != card["offer"] or lost:
                    raise SystemExit(f"Smarter Contact read-back failed on {v['id']}: lost {len(lost)} fields")
                print(f"  read back contact {v['id'][:8]}: deal fields present, no field dropped")
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"  contact {v['id'][:8]}: {str(e)[:120]}")
        if n and n % 100 == 0:
            state["sc"] = sorted(seen)
            DEALS_STATE.write_text(json.dumps(state), encoding="utf-8")
            print(f"  {n} contacts updated", flush=True)
        time.sleep(0.2)
    state["sc"] = sorted(seen)
    DEALS_STATE.write_text(json.dumps(state), encoding="utf-8")
    print(f"Smarter Contact: {len(seen)} land contacts carry the deal fields")
    return 0


def phase_test(args) -> int:
    """One contact for --phone with a real lot's parcel fields (no Sift Record, so a reply routes nowhere),
    in group 'Land Test', for the test text that proves the merge tags render."""
    sc = _sc()
    ids, keys = _fields(sc)
    rows = [r for r in land_rows().values() if r["lane"] in ("lot", "homesite")
            and not str(r.get("property_address") or "")[:1].isdigit()]
    row = rows[0]
    vals = {"ADDRESS": row["property_address"], "CITY": row.get("property_city") or "Knoxville", "STATE": "TN",
            "ASSESSOR_PARCEL_NUMBER": parcel_display(row["parcel_number"]), "PARCEL_ACRES": acres_display(row["acres"]),
            "PARCEL_COUNTY": COUNTY.get(str(row["fips"]), ""), "PARCEL_STATE": "TN"}
    inp = {"phone": "+1" + digits10(args.phone), "firstName": "Ty", "lastName": "Test",
           "attrs": [{"fieldId": keys[k], "value": v} for k, v in vals.items() if k in keys]}
    print("test contact:", {k: v for k, v in vals.items()})
    if not args.commit:
        print("DRY RUN: nothing created.")
        return 0
    c = sc.gql(M_CONTACT, {"input": inp})["createContact"]
    try:
        gid = _group(sc, "Land Test")["id"]
    except SystemExit:
        gid = sc.gql(M_GROUP, {"input": {"name": "Land Test"}})["createGroup"]["id"]
    sc.gql(M_ADD, {"input": {"groupId": gid, "ids": [c["id"]]}})
    time.sleep(2)
    print(f"created contact {c['id']} in 'Land Test' ({_group(sc, 'Land Test').get('contacts')} contacts)")
    return 0


SC_PUSH = "/api/internal/property/smarter_contact/"   # what DataSift's own Send To > Smarter Contact posts


def phase_push(args) -> int:
    """A DataSift list -> a Smarter Contact group, over the route DataSift's Send To button calls
    (APIv2.sendPropertiesToSmarterContact in the app bundle: the records query plus `list_id`, which for this
    integration is the destination group's NAME). DataSift's bulk skip-trace route once ignored its scope and
    ran the whole account, so: the list must hold exactly --expect records, the group must not exist yet, and
    after the push the group may hold no more than 6 contacts per record or the run says so loudly."""
    import pull_by_ids as P
    from datasift_api_upload import Api
    api = Api()
    luid = P.list_uuid(api, args.list)
    if not luid:
        raise SystemExit(f"no DataSift list named {args.list!r}")
    n = P.list_count(api, luid)
    if not n or (args.expect and n != args.expect):
        raise SystemExit(f"list {args.list!r} holds {n} records, expected {args.expect}: refusing")
    sc = _sc()
    try:
        g = _group(sc, args.group)
        raise SystemExit(f"group {args.group!r} already exists with {g.get('contacts')} contacts: refusing a second push")
    except SystemExit as e:
        if "already exists" in str(e):
            raise
    # The app always sends a phone filter (every type ticked by default); without one the push lands an empty
    # group (probe 2026-11-10: {"count": 1} from DataSift, 0 contacts in Smarter Contact after 20 minutes).
    # Statuses are an include list, so DNC, wrong and dead numbers stay out.
    body = {"query": {"must": {"any_lists": [luid]}}, "list_id": args.group,
            "columns": {"phones": {"must": {"phone_type": args.types.split(","),
                                            "phone_status": args.statuses.split(",")}}}}
    print(f"push: list {args.list!r} ({n} records) -> Smarter Contact group {args.group!r}, phone types "
          f"{args.types}, statuses {args.statuses}")
    if not args.commit:
        print("DRY RUN: nothing sent. Rerun with --commit.")
        return 0
    print("  response:", json.dumps(api.call(SC_PUSH, "POST", body))[:300])
    seen = {}
    for _ in range(int(args.wait * 4)):
        time.sleep(15)
        for name in (args.group, args.group + "_landline"):
            try:
                seen[name] = _group(sc, name).get("contacts") or 0
            except SystemExit:
                pass
        if seen.get(args.group):
            break
    total = sum(seen.values())
    print(f"  groups: {seen}")
    if total > 6 * n:
        print(f"WARNING: {total} contacts for {n} records, more than 6 a record: the push may have ignored its scope")
        return 2
    return 0 if seen.get(args.group) else 1


def phase_qa(args) -> int:
    sc = _sc()
    ids, _ = _fields(sc)
    contacts = _contacts(sc, _group(sc, args.group)["id"])
    done = sum(1 for c in contacts if is_uuid(values_by_key(c.get("attrs"), ids).get(SIFT_KEY))
               and not is_uuid(values_by_key(c.get("attrs"), ids).get("ASSESSOR_PARCEL_NUMBER")))
    print(f"{args.group}: {len(contacts)} contacts, {done} carry the parcel in APN and the record in "
          f"Sift Record, {len(contacts) - done} not patched")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", choices=["field", "templates", "create", "group", "deals", "test", "push", "patch",
                                        "split", "qa"],
                    required=True)
    ap.add_argument("--lanes", default="lots,tracts", help="create: which land lanes")
    ap.add_argument("--limit", type=int, default=0, help="create: records per lane (0 = all)")
    ap.add_argument("--group-suffix", default="", help='create: suffix for the groups, e.g. "Late 1110" on a '
                    "pass after Touch 1 started (its own make-up campaign, never a group already sending)")
    ap.add_argument("--phone", default="", help="test: the number that gets the test contact")
    ap.add_argument("--list", default="", help="push: the DataSift list to send")
    ap.add_argument("--expect", type=int, default=0, help="push: the record count the list must hold")
    ap.add_argument("--wait", type=float, default=10, help="push: minutes to wait for the group to appear")
    ap.add_argument("--types", default="MOBILE,VOIP,UNKNOWN", help="push: phone types to send (no landlines)")
    ap.add_argument("--statuses", default="UNKNOWN,NO_ANSWER,CORRECT",
                    help="push: phone statuses to send (UNKNOWN = no status; DNC, WRONG and DEAD stay out)")
    ap.add_argument("--group", default="", help="the Smarter Contact group the DataSift push created")
    ap.add_argument("--rows", nargs="*", help="land row files (default: output/land_market/pilot/*.json)")
    ap.add_argument("--commit", action="store_true", help="write (dry by default)")
    ap.add_argument("--all", action="store_true", help="patch: every contact after the first read-back")
    ap.add_argument("--sender", default="Adriana", help="templates: the first name that signs the texts")
    ap.add_argument("--first", default="", help="deals: the record uuid to write and read back first")
    ap.add_argument("--refresh", action="store_true", help="deals: rewrite Smarter Contact fields already written")
    a = ap.parse_args(argv)
    if a.phase not in ("field", "templates", "create", "group", "deals", "test") and not a.group:
        ap.error("--group is required")
    return {"field": phase_field, "templates": phase_templates, "create": phase_create, "group": phase_group,
            "deals": phase_deals,
            "test": phase_test,
            "push": phase_push, "patch": phase_patch, "split": phase_split, "qa": phase_qa}[a.phase](a)


if __name__ == "__main__":
    sys.exit(main())
