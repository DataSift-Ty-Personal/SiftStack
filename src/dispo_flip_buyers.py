"""Dispo buyer engine: pending-flip owners -> ty+2 records -> principal skip trace.

The premise (Ty, 2026-08-20): a property whose last sale is an ACTIVE investor
transaction is owned RIGHT NOW by a buyer. Pull those properties into the CRM
and the sequential call/text flows dial people who provably buy in this market.

THE SEMANTIC TRAP THIS SCRIPT EXISTS TO AVOID (verified live on Knox samples):
SiftMap's filter key is `extra_last_sale_investor_transaction_type` and it
labels the LAST SALE, not the current owner.
  * "pending"  = investor bought, exit pending. Current owner IS the investor
                 (VOLHOMES LLC, WEAVER DOORS LLC on the live sample). This is
                 the bucket that gets pulled into records.
  * "flip"     = the flip EXIT was the last sale. Current owner is the RETAIL
                 homebuyer (Christine Korf, bought from GDP PROPERTIES LLC).
                 Texting these records reaches ordinary homeowners. The flipper
                 is the last-sale SELLER, so this bucket feeds the ranked
                 flipper sweep (--phase flippers), never a record pull.
The key takes a LIST (["pending"]); a comma string 400s ("Expected a list").
Options: pending / wholesale / wholetail / flip / rental (from the live
SiftMapPage bundle, module 47865 FINANCIAL_DETAILS).

Sizes measured 2026-08-20: Knox pending 1,654 / flip 2,023; Blount pending 430
/ flip 329. Most pending records are ALREADY in ty+2 as old acquisition leads,
so their CRM owner is the PREVIOUS seller. The pull deliberately keeps
replace_owners False (doctrine: never blanket-overwrite resolved owners);
--phase trace writes the verified buyer + phones per record instead.

Phases (DRY by default, --commit to write):
  infra    list + tags + SiftMap auto-add presets + CRM sequential presets
  pull     bulk add pending properties (Knox + Blount) into the Dispo list
  trace    per record: SiftMap detail -> entity principal (reverse-address
           unmask first, Enformion BusinessV2 on miss) -> Enformion person
           search phones -> upsert-phones + note + dispo_traced tag
  flippers sweep the "flip" bucket's last-sale SELLERS into a ranked CSV of
           exited flippers (contactable via their own mailing address)

    python src/dispo_flip_buyers.py --phase infra
    python src/dispo_flip_buyers.py --phase infra --commit
    python src/dispo_flip_buyers.py --phase pull --commit
    python src/dispo_flip_buyers.py --phase trace --limit 5 --commit
    python src/dispo_flip_buyers.py --phase flippers --limit 300

Cost gates: trace is entity-owners-first (person-owned records can go through
DataSift's native unlimited skip trace); reverse-address unmask is free;
BusinessV2 and person search are billed per match (~$0.10, misses free).
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("REISIFT_ACCOUNT", "datasift-apikey")
_API_CLIENTS = Path(r"C:\Users\Tyrus\OneDrive\Desktop\Deal Room Coaching Call\_api\clients")
sys.path.insert(0, str(_API_CLIENTS))

from datasift_api_upload import Api  # noqa: E402

MAP = "https://map.reisift.io"
K_TXN = "extra_last_sale_investor_transaction_type"

LIST_TITLE = "Dispo - Flip Buyers"
FOLDER_TITLE = "21. Dispo Sequential Marketing"
TAG_ANCHOR = "Dispo Buyer"
# Ty 2026-08-20: NO underscore tags, mirror the Priority 1 system exactly.
# P1 presets are COUNTER-driven (predictivecall_attempts [1,1] etc.), not
# tag-driven, so the only extra tag is the trace bookkeeping one.
TAGS_FLOW = ["Dispo Traced"]

WORLD = [{"lon": -17.89461189115002, "lat": 72.08452694723852},
         {"lon": -17.89461189115002, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": -13.881763595427103},
         {"lon": -163.30518783395442, "lat": 72.08452694723852}]
COUNTIES = {"47093": ("Knox", "TN"), "47009": ("Blount", "TN")}

ENTITY_RX = re.compile(
    r"\bLLC\b|\bLLP\b|\bL\.?P\.?\b|\bINC\b|\bCORP\b|\bTRUST\b|\bPROPERTIES\b"
    r"|\bHOLDINGS\b|\bHOMES\b|\bCAPITAL\b|\bINVEST|\bVENTURES\b|\bGROUP\b|\bREALTY\b",
    re.I)

STATE_DIR = Path(os.getenv("SIFTSTACK_OUTPUT_DIR", "output"))
TRACE_STATE = STATE_DIR / "dispo_trace_state.json"


def county_address(fips: str, *, rich: bool) -> dict:
    name, st = COUNTIES[fips]
    title = "%s County, %s" % (name, st)
    base = {"state": st, "title": title, "value": title, "county": name,
            "searchType": "county",
            "counties": [{"fips": fips, "county_name": name}]}
    if rich:
        return base
    return dict(base, search=name, type="county")


def jwt_account(token: str) -> str:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("account", "")


class Client:
    """map.reisift.io + apiv2 internal, 429-aware, single-threaded."""

    def __init__(self):
        self.api = Api()
        self.account = jwt_account(self.api.token)

    def map_post(self, path: str, body: dict, *, timeout: int = 240):
        data = json.dumps(body).encode()
        for attempt in range(5):
            req = urllib.request.Request(
                MAP + path, data=data, method="POST",
                headers={"Authorization": "Bearer " + self.api.token,
                         "Content-Type": "application/json",
                         "accept": "application/json",
                         "origin": "https://beta.reisift.io",
                         "referer": "https://beta.reisift.io/"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt < 4:
                    self.api._mint()
                    continue
                if e.code in (429, 500, 502, 504) and attempt < 4:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError("HTTP %s on %s: %s"
                                   % (e.code, path, e.read().decode()[:300]))
        raise RuntimeError("gave up on " + path)

    def internal(self, path: str, method: str = "GET", body=None, tries: int = 6,
                 override: str | None = None):
        for _ in range(tries):
            time.sleep(0.45)  # /api/internal/ throttles hard; stay under it
            try:
                if override:
                    # The records search is POST-with-GET-semantics and needs
                    # the x-http-method-override header; a bare POST to
                    # /api/internal/property/ would try to CREATE a record.
                    data = json.dumps(body).encode() if body is not None else None
                    req = urllib.request.Request(
                        "https://apiv2.reisift.io" + path, data=data, method=method,
                        headers={"Authorization": "Bearer " + self.api.token,
                                 "Content-Type": "application/json",
                                 "x-http-method-override": override})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        t = r.read().decode()
                        return json.loads(t) if t else {}
                return self.api.call(path, method, body)
            except (RuntimeError, urllib.error.HTTPError) as e:
                # HTTPError before URLError: it subclasses URLError and must
                # reach the 429 handler, not the network-retry clause.
                if isinstance(e, urllib.error.HTTPError):
                    txt = e.read().decode("utf-8", "replace")
                    if e.code != 429:
                        raise RuntimeError("HTTP %s on %s: %s" % (e.code, path, txt[:200]))
                else:
                    txt = str(e)
                    if "429" not in txt:
                        raise
                m = re.search(r"available in (\d+)", txt)
                wait = min((int(m.group(1)) if m else 60) + 3, 300)
                print("      rate limited, sleeping %ss" % wait, flush=True)
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                # connection reset / DNS blip: a 20-minute sweep must survive
                # one dropped socket (killed a live run at 700/2082)
                print("      network error, retrying in 15s: %s" % str(e)[:80],
                      flush=True)
                time.sleep(15)
        raise RuntimeError("rate-limit retries exhausted on " + path)

    def search(self, fips: str, txn_types: list[str], *, index: int = 1,
               extra: dict | None = None) -> dict:
        f = {"type_single_family": True, K_TXN: list(txn_types)}
        f.update(extra or {})
        return self.map_post("/properties/search/", {
            "result_index": index, "with_boundaries": False, "filters": f,
            "addresses": [county_address(fips, rich=False)], "polygon": WORLD})

    def usage(self) -> tuple[int, int]:
        r = self.internal("/api/internal/upload/usage/", "POST", {})
        return r.get("upload_usage", 0), r.get("upload_limit", 0)


# ── phase: infra ──────────────────────────────────────────────────────

def crm_lists(c: Client) -> dict:
    r = c.internal("/api/internal/list/?limit=999")
    return {(l.get("title") or "").strip(): l["uuid"]
            for l in (r.get("results") or r.get("data") or [])}


def crm_tags(c: Client) -> dict:
    r = c.internal("/api/internal/tag/?limit=999")
    return {(t.get("title") or "").strip(): t["uuid"]
            for t in (r.get("results") or r.get("data") or [])}


def dispo_presets(list_uuid: str, tag: dict) -> list[dict]:
    """Mirror of the Priority 1 preset system (Ty: cut and dry, no tag flow).

    P1's grammar, verified from the live 'Hottest - *' presets: progression is
    driven by the DIALER's own counter (predictivecall_attempts [0,0] ready,
    [1,1] attempt 1, ...) and skiptraced/phone flags - no bookkeeping tags.
    Text lane runs the same way on sms_attempts (live counter, 415 records
    carry sends). Text and call are PARALLEL lanes, like P1's CALL and MAIL
    folders. must_not 'recently sold' self-cleans records whose flip exits.
    """
    L = [list_uuid]
    NOT_SOLD = {"any_tags": [tag["recently sold"]],
                "any_property_status": ["sold", "not_interested"]}
    return [
        {"title": "Dispo - 00 Needs Skipped",
         "must": {"any_lists": L, "phone": 0, "skiptraced": 0,
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 01 Skipped No Numbers",
         "must": {"any_lists": L, "phone": 0, "skiptraced": 1,
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 02 Ready to Text",
         "must": {"any_lists": L, "phone": 1, "sms_attempts": [0, 0],
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 03 Ready to Call",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [0, 0],
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 04 Call Attempt 1",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [1, 1],
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 05 Call Attempt 2",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [2, 2],
                  "must_not": dict(NOT_SOLD)}},
        {"title": "Dispo - 06 Call Attempt 3",
         "must": {"any_lists": L, "phone": 1,
                  "predictivecall_attempts": [3, 3],
                  "must_not": dict(NOT_SOLD)}},
    ]


def phase_infra(c: Client, commit: bool):
    lists = crm_lists(c)
    list_uuid = lists.get(LIST_TITLE)
    if not list_uuid:
        print("creating list %r" % LIST_TITLE)
        if commit:
            list_uuid = c.internal("/api/internal/list/", "POST",
                                   {"title": LIST_TITLE}).get("uuid")
        else:
            list_uuid = "DRY-RUN-UUID"
    else:
        print("list %r exists" % LIST_TITLE)

    tags = crm_tags(c)
    for title in [TAG_ANCHOR] + TAGS_FLOW:
        if title in tags:
            continue
        print("creating tag %r" % title)
        if commit:
            tags[title] = c.internal("/api/internal/tag/", "POST",
                                     {"title": title}).get("uuid")
        else:
            tags[title] = "DRY-RUN-UUID"

    # SiftMap auto-add feeders: new pending flips keep flowing in by name.
    # GET /filters/ pages at 10 rows and IGNORES ?limit= - follow `next` or
    # the exists-check lies and a re-run creates duplicates (seen live: 9319).
    existing = set()
    try:
        url = MAP + "/filters/"
        while url:
            req = urllib.request.Request(
                url, method="GET",
                headers={"Authorization": "Bearer " + c.api.token,
                         "accept": "application/json",
                         "origin": "https://beta.reisift.io",
                         "referer": "https://beta.reisift.io/"})
            with urllib.request.urlopen(req, timeout=60) as r:
                page = json.loads(r.read())
            existing |= {p.get("name") for p in page.get("results", [])}
            url = page.get("next")
    except Exception as e:
        print("WARN could not list map presets: %s" % e)

    for fips in COUNTIES:
        name = "Dispo - Pending Flips - %s" % COUNTIES[fips][0]
        if name in existing:
            print("map preset %r exists" % name)
            continue
        print("creating map preset %r (auto-add ON)" % name)
        if not commit:
            continue
        c.map_post("/filters/", {
            "name": name,
            "description": "Active investor purchases (exit pending): current "
                           "owner is a buyer. Feeds %s." % LIST_TITLE,
            "auto_add_enabled": True, "replace_owners_enabled": False,
            "lists": [LIST_TITLE], "tags": [TAG_ANCHOR],
            "filter_data": {"filters": {"type_single_family": True,
                                        K_TXN: ["pending"]},
                            "addresses": [county_address(fips, rich=True)]}})

    # CRM sequential presets.
    folders = c.internal("/api/internal/filter-preset-folder/?type=properties"
                         "&limit=999").get("results", [])
    folder = next((f for f in folders
                   if (f.get("title") or "").upper() == FOLDER_TITLE.upper()), None)
    if not folder:
        print("creating folder %r" % FOLDER_TITLE)
        if commit:
            folder = c.internal("/api/internal/filter-preset-folder/", "POST",
                                {"title": FOLDER_TITLE, "type": "properties",
                                 "permissions": []})
    have = set()
    if folder and folder.get("uuid"):
        have = {p.get("title") for p in
                c.internal("/api/internal/filter-preset-folder/%s/filter-preset/"
                           "?limit=999" % folder["uuid"]).get("results", [])}

    # The flow presets anchor on the VIP list once it exists (never-contacted
    # buyers only, Ty's rule); before the first vip run they fall back to the
    # raw list so the flow is never empty-by-configuration.
    preset_list_uuid = lists.get(VIP_LIST_TITLE) or list_uuid
    for p in dispo_presets(preset_list_uuid, tags):
        if p["title"] in have:
            print("  preset %r exists" % p["title"])
            continue
        print("  creating preset %r" % p["title"])
        if not commit:
            continue
        c.internal("/api/internal/filter-preset/", "POST",
                   {"title": p["title"], "folder": folder["uuid"],
                    "quick_filter": False,
                    "filters": {"must": p["must"], "account": c.account},
                    "type": "properties"})

    if commit and folder and folder.get("uuid"):
        back = {p.get("title") for p in
                c.internal("/api/internal/filter-preset-folder/%s/filter-preset/"
                           "?limit=999" % folder["uuid"]).get("results", [])}
        missing = [p["title"] for p in dispo_presets(preset_list_uuid, tags)
                   if p["title"] not in back]
        if missing:
            raise SystemExit("presets missing after read-back: %s" % missing)
        print("read-back OK: %d presets in %r" % (len(back), FOLDER_TITLE))
    if not commit:
        print("\nDRY RUN: nothing created. Re-run with --commit.")


# ── phase: pull ───────────────────────────────────────────────────────

def phase_pull(c: Client, commit: bool):
    used, limit = c.usage()
    print("record allowance: %s used of %s (%s left)"
          % (f"{used:,}", f"{limit:,}", f"{limit - used:,}"))
    for fips in COUNTIES:
        name = COUNTIES[fips][0]
        n = c.search(fips, ["pending"]).get("total_results", 0)
        n_new = c.search(fips, ["pending"],
                         extra={"in_my_account_mode": "not_in"}).get("total_results", 0)
        print("%s pending: %s total, %s new to account" % (name, f"{n:,}", f"{n_new:,}"))
        if not commit:
            continue
        r = c.map_post("/properties/add-properties-by-query/", {
            "auto_add_enabled": False,
            "lists": [LIST_TITLE], "tags": [TAG_ANCHOR],
            # Doctrine: never blanket-overwrite owner data; --phase trace
            # writes the verified buyer per record instead.
            "replace_owners": False,
            "query": {"result_index": 1, "with_boundaries": False,
                      "filters": {"type_single_family": True, K_TXN: ["pending"]},
                      "addresses": [county_address(fips, rich=False)],
                      "polygon": WORLD}})
        print("  add submitted: %s" % (r if isinstance(r, str) else json.dumps(r)[:200]))
    if commit:
        print("\nAdds process ASYNC server-side; zero list growth after 20 min "
              "means check /api/internal/activity/, not that the add failed.")
    else:
        print("\nDRY RUN: nothing added. Re-run with --commit.")


# ── phase: trace ──────────────────────────────────────────────────────

def _clean_entity(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip()


def _owner_name(oi: dict) -> str:
    return _clean_entity(
        oi.get("owner_name")
        or " ".join(p for p in [oi.get("first_name"), oi.get("last_name")] if p)
        or oi.get("name") or "")


def _mail_line(oi: dict) -> str:
    # owner_info carries the mailing address as one string: owner_mail_address
    # (verified in buyer_sweep; there is no structured mailing dict).
    m = oi.get("owner_mail_address") or ""
    if isinstance(m, dict):
        m = ", ".join(p for p in [m.get("street"), m.get("city"),
                                  m.get("state"), m.get("zip")] if p)
    return (m or "").strip()


def load_trace_state() -> dict:
    if TRACE_STATE.exists():
        return json.loads(TRACE_STATE.read_text())
    return {"done": {}}


def phase_trace(c: Client, commit: bool, limit: int, entities_only: bool):
    from siftmap_api import SiftMapClient  # noqa: E402  (Deal Room _api)
    import enformion_business  # noqa: E402
    from enformion_heir import person_search, first_match  # noqa: E402
    from enformion_ftm import enf_phones, clean_owner_name  # noqa: E402

    sm = SiftMapClient()
    state = load_trace_state()
    lists = crm_lists(c)
    list_uuid = lists.get(LIST_TITLE)
    if not list_uuid:
        raise SystemExit("list %r missing - run --phase infra --commit first" % LIST_TITLE)
    tags = crm_tags(c)

    # Records in the Dispo list, via the internal records search.
    rows, offset = [], 0
    while True:
        r = c.internal("/api/internal/property/", "POST",
                       {"limit": 100, "offset": offset,
                        "query": {"must": {"any_lists": [list_uuid]}}},
                       override="GET")
        page = r.get("results") or r.get("data") or []
        rows.extend(page)
        total = r.get("count") or 0
        offset += 100
        if offset >= total or not page:
            break
    print("Dispo list records: %d (%d already traced)"
          % (len(rows), len(state["done"])))

    todo = [r for r in rows if r.get("uuid") not in state["done"]]
    traced = attempted = skipped_person = 0
    for rec in todo:
        if attempted >= limit:
            break
        uuid = rec.get("uuid")
        addr = rec.get("address") or {}
        street = addr.get("street") or ""
        city = addr.get("city") or ""
        st = addr.get("state") or "TN"
        line = "%s, %s, %s" % (street, city, st)

        # Current deed owner from SiftMap, never the (possibly stale) CRM owner.
        try:
            cands = sm.autocomplete(line)
            best = cands[0] if cands else None
            detail = sm.get_detail(best.get("dataflik_id") or best.get("id")) if best else {}
        except Exception as e:
            print("  %-34s SiftMap miss: %s" % (street[:34], str(e)[:80]))
            continue
        oi = detail.get("owner_info") or {}
        owner = _owner_name(oi)
        mail = _mail_line(oi)
        is_entity = bool(ENTITY_RX.search(owner))
        if entities_only and not is_entity:
            skipped_person += 1
            if commit:  # dry runs must not pollute the resume state
                state["done"][uuid] = {"skip": "person owner - native skip trace"}
            continue
        attempted += 1

        principal, how = "", ""
        if is_entity:
            # 1) FREE: the Harper move - if the entity's mailing address is a
            #    residence, its SiftMap owner is the human principal.
            if mail and not mail.upper().startswith("PO BOX"):
                try:
                    mc = sm.autocomplete(mail)
                    if mc:
                        moi = (sm.get_detail(mc[0].get("dataflik_id") or mc[0].get("id"))
                               .get("owner_info") or {})
                        cand = _owner_name(moi)
                        if cand and not ENTITY_RX.search(cand):
                            principal, how = cand, "reverse-address"
                except Exception:
                    pass
            # 2) PAID fallback: BusinessV2 officers.
            if not principal and commit:
                offs = enformion_business.find_principals(owner, "%s, %s" % (city, st))
                if offs:
                    principal = offs[0].get("name") or ""
                    how = "BusinessV2"
            # A "principal" that is itself a corporate front is not a dial
            # target (live leaks: C T CORPORATION SYSTEM, "FOR SERVICE OF
            # PROCESS...", US BANK TRUST as trustee). Writing its phones puts
            # an 800 line on the dial sheet.
            if principal and (ENTITY_RX.search(principal) or re.search(
                    r"SERVICE OF PROCESS|CORPORATION SYSTEM|REGISTERED AGENT"
                    r"|\bBANK\b|\bNATIONAL ASSOC", principal, re.I)):
                principal, how = "", ""
        else:
            principal, how = owner, "deed owner"

        phones: list[str] = []
        if principal and commit:
            first, last = clean_owner_name(principal)
            if first and last:
                anchor_city, anchor_zip = "", ""
                m = re.search(r",\s*([A-Za-z .]+),\s*([A-Z]{2})\s*(\d{5})?", mail or "")
                if m:
                    anchor_city, anchor_zip = m.group(1).strip(), m.group(3) or ""
                data = person_search(first, last, city=anchor_city or city,
                                     state=st, zip_code=anchor_zip)
                phones = enf_phones(first_match(data) or {})

        sale = (detail.get("sale_history") or [{}])[0]
        print("  %-34s owner=%-28s principal=%s (%s) phones=%d"
              % (street[:34], owner[:28], principal[:24] or "-", how or "-", len(phones)))

        if not commit:
            continue

        # Write back: phones on the record's owner + note + traced tag.
        try:
            det = c.internal("/api/internal/property/%s/" % uuid)
            owner_uuid = ((det.get("owner") or {}).get("uuid")
                          if isinstance(det.get("owner"), dict) else det.get("owner"))
            if phones and owner_uuid:
                c.internal("/api/internal/owner/%s/upsert-phones/" % owner_uuid,
                           "POST",
                           {"phones": [{"number": p, "type": "UNKNOWN",
                                        "tags": ["dispo"], "status": "UNKNOWN",
                                        "is_connected": True, "verified": False}
                                       for p in phones[:5]]})
            note = ("Dispo buyer: %s%s. Bought %s for $%s%s. Resolved via %s."
                    % (owner,
                       (" - principal %s" % principal) if principal and principal != owner else "",
                       sale.get("sale_date") or "?",
                       sale.get("sale_price") or "?",
                       " CASH" if sale.get("is_cash_sale") else "",
                       how or "deed"))
            c.internal("/api/internal/property/%s/add-notes/" % uuid,
                       "POST", {"notes": note[:2000]})
            # Tag via address-upsert: POST /property/ upserts by address, tags
            # ACCUMULATE, and omitting the owner key leaves the owner alone.
            try:
                c.internal("/property/", "POST",
                           {"address": {"street": street, "city": city,
                                        "state": st,
                                        "postal_code": addr.get("postal_code") or ""},
                            "tags": ["Dispo Traced"]})
            except Exception as e:
                print("    tag write failed (non-fatal): %s" % str(e)[:100])
            state["done"][uuid] = {"owner": owner, "principal": principal,
                                   "how": how, "phones": phones}
            traced += 1
        except Exception as e:
            print("    WRITE FAILED %s: %s" % (uuid, str(e)[:120]))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TRACE_STATE.write_text(json.dumps(state, indent=1))
    print("\n%d entity records attempted, %d written, %d person-owned skipped "
          "(native skip trace covers those); state -> %s"
          % (attempted, traced, skipped_person, TRACE_STATE))
    if not commit:
        print("DRY RUN: no paid lookups, no CRM writes. Re-run with --commit.")


# ── phase: vip ────────────────────────────────────────────────────────

VIP_LIST_TITLE = "Dispo - VIP Buyers"
FOLDER_TITLE_FINAL = "21. Dispo Sequential Marketing"
VIP_STATE = STATE_DIR / "dispo_vip_state.json"

# Lists DataSift attaches as property CLASSIFICATION at ingest (a brand-new
# record we added arrived already on "Absentee Owners"). Membership here says
# nothing about outreach. EVERY other list in the account is treated as a
# marketing list: membership means the record sits in a campaign universe,
# which is exactly what Ty wants kept out of the VIP flow ("they're probably
# on some of the lists, like a tired landlord").
CLASSIFICATION_LISTS = {
    LIST_TITLE, VIP_LIST_TITLE,
    "Absentee Owners", "Owner Occupied", "Free & Clear", "High Equity",
    "Low Equity", "Negative Equity", "Multi-Family", "Warehouses",
    "Recently Sold", "Arms-Length Transfers", "Vacant",
}

# Tag titles that record an outreach touch. The dispo flow's own tags are
# included so a VIP rebuild never re-admits someone the flow already texted.
OUTREACH_TAG_RX = re.compile(
    r"sms_sent|called_day|mailed|mms|text_touch|door.?knock|dispo_sms|dispo_called",
    re.I)


def _name_key(first: str = "", last: str = "", company: str = "") -> str:
    s = company or ("%s %s" % (first or "", last or ""))
    toks = sorted(re.sub(r"[^A-Za-z0-9 ]", " ", s.upper()).split())
    return " ".join(toks)


def _owner_key(o: dict) -> str:
    if not isinstance(o, dict):
        return ""
    return _name_key(o.get("first_name") or "", o.get("last_name") or "",
                     o.get("company") or "")


def phase_vip(c: Client, commit: bool):
    """Materialize the never-marketed VIP subset and point the flow at it.

    A record is VIP only if ALL of:
      * zero attempts on every channel (predictive call, SMS, RVM, direct
        mail) and never direct-mailed
      * no lead status ever set (any status, including sold, means a human
        or a sequence worked it)
      * no outreach tags
      * on no list beyond the classification set above
      * its OWNER's name does not appear on any marketing list member or any
        worked record anywhere in the account (the tired-landlord-owns-a-new-
        flip case: we texted them at property A, they now own flip B)
    """
    state = json.loads(VIP_STATE.read_text()) if VIP_STATE.exists() else {}
    lists = crm_lists(c)
    list_uuid = lists.get(LIST_TITLE)
    if not list_uuid:
        raise SystemExit("list %r missing" % LIST_TITLE)

    # -- 1. contacted-owner name set (account-wide, row sweep) ------------
    if "contacted_owners" not in state:
        contacted: set = set()
        marketing_uuids = [u for t, u in lists.items()
                           if t not in CLASSIFICATION_LISTS]
        print("sweeping %d marketing lists for member owner names..."
              % len(marketing_uuids))
        for i, mu in enumerate(marketing_uuids, 1):
            offset = 0
            while True:
                # The API hard-refuses offset+limit > 10000 ("Can't fetch
                # more than 10000 items!"). Cap and SAY what was dropped -
                # the record-level gate still sees those records directly.
                if offset >= 10000:
                    print("  TRUNCATED list %s at 10,000 of %s rows"
                          % (mu[:8], f"{r.get('count', 0):,}"))
                    break
                r = c.internal("/api/internal/property/", "POST",
                               {"limit": 100, "offset": offset,
                                "query": {"must": {"any_lists": [mu]}}},
                               override="GET")
                rows = r.get("results") or []
                for row in rows:
                    k = _owner_key(row.get("owner") or {})
                    if k:
                        contacted.add(k)
                offset += 100
                if offset >= (r.get("count") or 0) or not rows:
                    break
            if i % 10 == 0:
                print("  %d/%d lists, %d owner names"
                      % (i, len(marketing_uuids), len(contacted)))
        # Worked records anywhere (status set or mailed), list or no list.
        # An empty must 400s ("Filter can't be empty"), so query the worked
        # populations directly instead of sweeping the whole account.
        sts = c.internal("/api/internal/status/?limit=999")
        slugs = [s.get("slug") or s.get("title")
                 for s in (sts.get("results") or sts.get("data") or [])]
        for label, must in [("status set", {"any_property_status": slugs}),
                            ("mailed", {"directmail_attempts": [1, 999999]})]:
            print("sweeping worked records (%s)..." % label)
            offset = 0
            while True:
                if offset >= 10000:
                    print("  TRUNCATED %s sweep at 10,000 of %s rows"
                          % (label, f"{r.get('count', 0):,}"))
                    break
                r = c.internal("/api/internal/property/", "POST",
                               {"limit": 100, "offset": offset,
                                "query": {"must": must}}, override="GET")
                rows = r.get("results") or []
                for row in rows:
                    k = _owner_key(row.get("owner") or {})
                    if k:
                        contacted.add(k)
                offset += 100
                if offset >= (r.get("count") or 0) or not rows:
                    break
        state["contacted_owners"] = sorted(contacted)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        VIP_STATE.write_text(json.dumps(state))
    contacted = set(state["contacted_owners"])
    print("contacted-owner names known: %d" % len(contacted))

    # -- 2. classify every dispo record off its full detail ---------------
    verdicts = state.setdefault("verdicts", {})
    rows, offset = [], 0
    while True:
        r = c.internal("/api/internal/property/", "POST",
                       {"limit": 100, "offset": offset,
                        "query": {"must": {"any_lists": [list_uuid]}}},
                       override="GET")
        page = r.get("results") or []
        rows.extend(page)
        offset += 100
        if offset >= (r.get("count") or 0) or not page:
            break
    todo = [r for r in rows if r["uuid"] not in verdicts]
    print("dispo records: %d (%d already classified)" % (len(rows), len(verdicts)))
    for i, row in enumerate(todo, 1):
        det = c.internal("/api/internal/property/%s/" % row["uuid"])
        reasons = []
        for f in ("predictivecall_attempts", "sms_attempts",
                  "rvm_attempts", "directmail_attempts"):
            if (det.get(f) or 0) > 0:
                reasons.append(f)
        if det.get("direct_mailed"):
            reasons.append("direct_mailed")
        if det.get("status"):
            reasons.append("status:%s" % det["status"])
        bad_tags = [t.get("title") if isinstance(t, dict) else str(t)
                    for t in (det.get("tags") or [])
                    if OUTREACH_TAG_RX.search(
                        t.get("title") if isinstance(t, dict) else str(t))]
        if bad_tags:
            reasons.append("tags:%s" % ",".join(bad_tags[:3]))
        mkt = [l.get("title") if isinstance(l, dict) else str(l)
               for l in (det.get("lists") or [])
               if (l.get("title") if isinstance(l, dict) else str(l))
               not in CLASSIFICATION_LISTS]
        if mkt:
            reasons.append("lists:%s" % ",".join(mkt[:4]))
        ok = _owner_key(det.get("owner") or {})
        if ok and ok in contacted:
            reasons.append("owner_contacted_elsewhere")
        verdicts[row["uuid"]] = {"vip": not reasons, "why": reasons,
                                 "street": (det.get("address") or {}).get("street"),
                                 "city": (det.get("address") or {}).get("city"),
                                 "zip": (det.get("address") or {}).get("postal_code")}
        if i % 100 == 0:
            VIP_STATE.write_text(json.dumps(state))
            n_vip = sum(1 for v in verdicts.values() if v["vip"])
            print("  %d/%d classified, %d VIP so far" % (i, len(todo), n_vip))
    VIP_STATE.write_text(json.dumps(state))

    vip = [u for u, v in verdicts.items() if v["vip"]]
    from collections import Counter
    top = Counter(w.split(":")[0] for v in verdicts.values()
                  for w in v["why"]).most_common(8)
    print("\nVIP: %d of %d. Top exclusion reasons: %s"
          % (len(vip), len(verdicts), top))
    if not commit:
        print("DRY RUN: no list writes, no preset changes. Re-run with --commit.")
        return

    # -- 3. materialize the VIP list --------------------------------------
    vip_uuid = lists.get(VIP_LIST_TITLE)
    if not vip_uuid:
        vip_uuid = c.internal("/api/internal/list/", "POST",
                              {"title": VIP_LIST_TITLE}).get("uuid")
        print("created list %r" % VIP_LIST_TITLE)
    added = state.setdefault("vip_added", [])
    for u in vip:
        if u in added:
            continue
        v = verdicts[u]
        # address-upsert: lists accumulate, owner key omitted = untouched
        c.internal("/property/", "POST",
                   {"address": {"street": v["street"], "city": v["city"],
                                "state": "TN", "postal_code": v["zip"] or ""},
                    "lists": [VIP_LIST_TITLE]})
        added.append(u)
        if len(added) % 100 == 0:
            VIP_STATE.write_text(json.dumps(state))
            print("  %d/%d added to VIP list" % (len(added), len(vip)))
    VIP_STATE.write_text(json.dumps(state))

    # -- 4. re-point the 5 presets at the VIP list (read-modify-write) ----
    folders = c.internal("/api/internal/filter-preset-folder/?type=properties"
                         "&limit=999").get("results", [])
    folder = next((f for f in folders if (f.get("title") or "").upper()
                   in (FOLDER_TITLE.upper(), FOLDER_TITLE_FINAL.upper())), None)
    if not folder:
        raise SystemExit("dispo preset folder missing")
    presets = c.internal("/api/internal/filter-preset-folder/%s/filter-preset/"
                         "?limit=999" % folder["uuid"]).get("results", [])
    for p in presets:
        d = c.internal("/api/internal/filter-preset/%s/" % p["uuid"])
        must = (d.get("filters") or {}).get("must") or {}
        if must.get("any_lists") == [vip_uuid]:
            continue
        must["any_lists"] = [vip_uuid]
        c.internal("/api/internal/filter-preset/%s/" % p["uuid"], "PATCH",
                   {"filters": {"must": must, "account": c.account}})
        back = c.internal("/api/internal/filter-preset/%s/" % p["uuid"])
        got = ((back.get("filters") or {}).get("must") or {}).get("any_lists")
        if got != [vip_uuid]:
            raise SystemExit("preset %r did not take the VIP list" % p["title"])
        print("  preset %r -> VIP list" % p["title"])

    # -- 5. move the folder to the end of the stack (folders sort by title)
    if (folder.get("title") or "") != FOLDER_TITLE_FINAL:
        c.internal("/api/internal/filter-preset-folder/%s/" % folder["uuid"],
                   "PATCH", {"title": FOLDER_TITLE_FINAL})
        print("folder renamed -> %r" % FOLDER_TITLE_FINAL)


# ── phase: flippers ───────────────────────────────────────────────────

def phase_flippers(c: Client, limit: int, months: int = 24):
    """Exited flippers = last-sale SELLERS of the 'flip' bucket. Free sweep.

    Restricted to exits in the last `months` (an exit from 2021 says little
    about who is buying today).
    """
    from datetime import date, timedelta
    from siftmap_api import SiftMapClient  # noqa: E402
    sm = SiftMapClient()
    since = (date.today() - timedelta(days=months * 30)).isoformat()
    out = defaultdict(lambda: {"n": 0, "sales": [], "counties": set()})
    pulled = 0
    for fips in COUNTIES:
        cname = COUNTIES[fips][0]
        idx = 1
        while pulled < limit:
            r = c.search(fips, ["flip"], index=idx,
                         extra={"extra_last_sale_date_min": since})
            page = r.get("data") or []
            if not page:
                break
            for row in page:
                if pulled >= limit:
                    break
                pid = row.get("dataflik_id") or row.get("id")
                try:
                    d = sm.get_detail(pid)
                except Exception:
                    continue
                pulled += 1
                sale = (d.get("sale_history") or [{}])[0]
                seller = _clean_entity(sale.get("seller_name") or "")
                if not seller:
                    continue
                key = seller.upper()
                out[key]["n"] += 1
                out[key]["counties"].add(cname)
                out[key]["sales"].append(
                    # the search ROW carries the full address string; the
                    # detail record has no street field at all
                    {"addr": row.get("address"),
                     "date": sale.get("sale_date"), "price": sale.get("sale_price")})
            idx += len(page)
            got = r.get("total_results", 0)
            if idx > got:
                break
    ranked = sorted(out.items(), key=lambda kv: -kv[1]["n"])
    path = STATE_DIR / "dispo_flippers_ranked.csv"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["flipper", "exits_seen", "counties", "recent_sales"])
        for name, v in ranked:
            w.writerow([name, v["n"], "/".join(sorted(v["counties"])),
                        "; ".join("%s %s $%s" % (s["addr"], s["date"], s["price"])
                                  for s in v["sales"][:3])])
    print("swept %d flip exits -> %d distinct flippers -> %s"
          % (pulled, len(ranked), path))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phase", required=True,
                    choices=["infra", "pull", "trace", "vip", "flippers"])
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--all-owners", action="store_true",
                    help="trace person-owned records too (default: entities only)")
    ap.add_argument("--months", type=int, default=24,
                    help="flippers sweep: only exits in the last N months")
    args = ap.parse_args()

    c = Client()
    print("account: %s (%s)\n" % (c.api.email, c.account[:8]))
    if c.api.email.split("@")[0] != "ty+2":
        raise SystemExit("expected ty+2 credentials in .env, got %s - REFUSING"
                         % c.api.email)

    if args.phase == "infra":
        phase_infra(c, args.commit)
    elif args.phase == "pull":
        phase_pull(c, args.commit)
    elif args.phase == "trace":
        phase_trace(c, args.commit, args.limit, entities_only=not args.all_owners)
    elif args.phase == "vip":
        phase_vip(c, args.commit)
    elif args.phase == "flippers":
        phase_flippers(c, args.limit, months=args.months)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
