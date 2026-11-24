"""Contacts for the buyers a buyer search found: who decides at each one, and the numbers that reach them.

Roma's buyer_search ranks who buys a house; this finds how to reach every one of them, cheapest source first:

  seed      the buyer search's buyers, rebuilt from the house's kept deeds, as deal_buyer_dp's ranked.json (free).
            Each is joined to the dispo registry (its principal, its scored numbers, its DataSift record); a
            person buyer, or a trust named for one, is its own decision maker.
  houses    the houses each buyer still holds, found in DataSift by address (free). A record counts only when its
            owner IS the buyer: a pending flip pulled in earlier often still carries the seller as its owner, and
            the seller's numbers are not the buyer's.
  research  open research on every buyer (the research engine's low-cost search-and-read loop): who owns or runs it,
            and a published phone, email or site. A person counts only when a page the model read names both the
            person and the company; a number or an email only when the page shows it.
  officers  Enformion BusinessV2 for companies still without a named person ($0.10 a match, a miss is free),
            in-state officers only (an officer in another state is a different company).
  contacts  deal_buyer_dp on all of them: their DataSift records, the dossier, SmartSkip at a verified home street,
            Trestle Real Contact on each (decision maker, number) pair, then the workbook.

Read only toward DataSift. DRY by default: research, officers and contacts spend only with --commit.

    python src/buyer_contacts.py --slug 3014-sampleville --work <house folder> --record <uuid> --arv 225000 --phase seed
    python src/buyer_contacts.py --slug 3014-sampleville --phase houses
    python src/buyer_contacts.py --slug 3014-sampleville --phase research --commit
    python src/buyer_contacts.py --slug 3014-sampleville --phase officers --commit
    python src/buyer_contacts.py --slug 3014-sampleville --phase contacts --commit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("buyer_contacts")

ROOT = Path(__file__).resolve().parent.parent
HOLD_ROLES = ("holder", "entity buy")
MAX_NUMBERS = 4            # numbers per buyer put to the name check, best first
MAX_DMS = 2                # decision makers per buyer traced and name-checked
SEARCHES, PAGES = 6, 5     # one buyer's research budget
GENERIC = {"LLC", "INC", "CORP", "CO", "COMPANY", "LP", "LLP", "PLLC", "THE", "AND", "OF", "PROPERTIES", "PROPERTY",
           "HOMES", "HOME", "HOUSES", "INVESTMENTS", "INVESTMENT", "INVESTORS", "HOLDINGS", "GROUP", "CAPITAL", "REAL",
           "ESTATE", "REALTY", "TRUST", "MANAGEMENT", "ENTERPRISES", "ENTERPRISE", "DEVELOPMENT", "DEVELOPERS",
           "SOLUTIONS", "PARTNERS", "VENTURES", "ACQUISITIONS", "ASSETS", "ASSET", "BUILDERS", "CONSTRUCTION",
           "RENTALS", "SERVICES", "TN", "KNOXVILLE", "TENNESSEE", "FAMILY", "REVOCABLE", "LIVING"}
BAD_STATUS = ("WRONG", "DNC", "CORRECT_DNC", "WRONG_DNC", "DEAD")
TIER_RANK = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3, "Drop": 5}


def _load(p: Path, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(p: Path, obj) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def load_env(path: Path | None = None) -> None:
    """The keys live in the main checkout's .env; a worktree has none. Never overrides a set variable."""
    for cand in [path, ROOT / ".env", Path(os.environ.get("SIFTSTACK_ENV") or "")]:
        if cand and Path(cand).is_file():
            for line in Path(cand).read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


def norm(name: str) -> str:
    from enterprise_prospects import norm_name
    return norm_name(name or "")


def distinctive(name: str) -> set[str]:
    return {w for w in norm(name).split() if len(w) > 2 and w not in GENERIC}


def digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""


# ----------------------------------------------------------------------------- seed

def rebuild(work: Path, subject: dict, *, months: int = 24, today: date | None = None, arv=None) -> dict:
    """The buyer search's answer, rebuilt from a house folder's kept deeds with no network: the half of
    open_api.find_buyers that runs after the deed reads (the sweep kept there is already cut to houses like this
    one)."""
    import argparse as _ap
    from buyer_search import client, engine, open_api
    dbm = open_api._dbm()
    today = today or date.today()
    sw = _load(Path(work) / "sweep.json", {})
    det = _load(Path(work) / "details.json", {})
    if not sw:
        raise SystemExit("no sweep.json in %s" % work)
    a = _ap.Namespace(lat=float(subject.get("lat") or 0), lon=float(subject.get("lon") or 0))
    inv, comps = sw.get("investor") or {}, sw.get("comps") or []
    houses = {}
    for r in comps + [r for rows in inv.values() for r in rows]:
        f = open_api.row_fields(r)
        f["year_built"] = open_api._num((det.get(r["id"]) or {}).get("year_built"))
        houses[r["id"]] = f
    bnd = open_api.bands(subject, today.year)
    lo_day = date.fromordinal(today.toordinal() - int(months * 30.44)).isoformat()
    kept = []
    for o in dbm._observations(a, sw, det):
        if open_api.is_subject(o.get("id"), o.get("address"), subject):
            continue
        y = (houses.get(o["id"]) or {}).get("year_built")
        if "year_built" in bnd and (y is None or not (bnd["year_built"][0] <= y <= bnd["year_built"][1])):
            continue
        if max(str(o.get("date") or ""), str(o.get("exit_date") or "")) < lo_day:
            continue
        kept.append(o)
    rows = open_api.buyer_rows(subject, kept, det, houses, months)
    for r, s in zip(rows, engine.score_buyers(rows, engine.WEIGHTS)):
        r["score"] = s
    rows = sorted((r for r in rows if r["score"] > engine.Params().min_score), key=lambda r: (-r["score"], r["buyer_id"]))
    result = {"subject": subject, "buyers": rows}
    return open_api.price_guesses(client.annotate(result), arv)


def registry_index(reg_dir: Path) -> tuple[dict, dict]:
    reg = _load(Path(reg_dir) / "registry.json", [])
    by_key, by_mail = {}, {}
    for r in reg:
        by_key[r["buyer_key"]] = r
        for al in r.get("aliases") or []:
            by_key.setdefault(norm(al), r)
        if r.get("mail"):
            by_mail.setdefault(mail_key(r["mail"]), r)
    return by_key, by_mail


def mail_key(s: str) -> str:
    return " ".join((s or "").upper().replace(",", " ").split()[:3])


def mail_of(b: dict) -> str:
    st = " ".join(x for x in (b.get("buyer_state"), b.get("buyer_zip")) if x)
    return ", ".join(x for x in (b.get("buyer_address"), b.get("buyer_city"), st) if x)


COMPANY_WORDS = re.compile(r"\b(llc|llp|inc|corp|co|company|investments?|propert(y|ies)|real estate|realty|homes?|"
                           r"holdings?|group|capital|ventures?|enterprises?|development|developers?|builders?|"
                           r"construction|rentals?|partners|management|acquisitions?|solutions|services|collective|"
                           r"assets?|trust|fund|bank|church)\b", re.I)


def looks_like_company(b: dict) -> bool:
    """The deed's company flag misses names like "Revive Collective Real Estate Investments"."""
    return bool(b.get("is_buyer_corporation")) or bool(COMPANY_WORDS.search(b.get("buyer_full_name") or ""))


def own_decision_maker(b: dict) -> dict | None:
    """A person buyer decides for itself; a trust named for a person ("Larry V Bailey Revocable Living Trust") is
    that person's. "Koontz Family Revocable Trust" names nobody."""
    name = (b.get("buyer_full_name") or "").strip()
    if not name:
        return None
    if not looks_like_company(b):
        toks = name.split()
        role = "the buyer"
        # A deed writes a person LAST FIRST in capitals ("HERNANDEZ KAYLA"); SiftMap's own owner names come title
        # case and first name first ("Daniel C Burton"). A trace or a name check on the wrong order finds nobody.
        if name.isupper() and 2 <= len(toks) <= 3:
            name = " ".join(t.title() for t in toks[1:] + toks[:1])
            role = "the buyer (the deed's last-first order turned around)"
        return {"name": name, "role": role, "confidence": "high", "address_or_city": mail_of(b),
                "from": "deed", "evidence": [{"url": "deed", "quote": "the buyer of record on the deed"}]}
    m = re.match(r"^(.+?)\s+(revocable|irrevocable|living|family)\b.*\btrust?\b", name, re.I)
    if m:
        who = m.group(1).strip()
        toks = [t for t in re.split(r"\s+", who) if t]
        if len(toks) >= 2 and not re.search(r"\b(llc|inc|corp)\b", who, re.I):
            return {"name": who, "role": "trustee (named in the trust)", "confidence": "medium",
                    "address_or_city": mail_of(b), "from": "deed", "evidence": [{"url": "deed", "quote": name}]}
    return None


def registry_decision_maker(reg: dict, principals: dict) -> dict | None:
    """The registry's principal (the owner of the house at the company's mailing address, read in August).
    deal_buyer_dp reads principals only from the 2+ purchase profiles, so this carries the rest in."""
    pr = principals.get(reg.get("buyer_key") or "") or {}
    if not pr.get("principal"):
        return None
    return {"name": pr["principal"], "role": "owner of the company's mailing address", "confidence": "medium",
            "address_or_city": reg.get("mail") or "", "from": "registry",
            "evidence": [{"url": "registry", "quote": pr.get("principal_source") or "dispo registry"}]}


def research_file(out: Path, b: dict) -> Path:
    key = b.get("buyer_id") or b.get("buyer_full_name") or b.get("buyer") or "x"
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")
    return Path(out) / "research" / ("%s.json" % slug[:70])


def phase_seed(a, out: Path) -> None:
    subject = _load(out / "subject.json", {})
    if a.record or a.subject_json:
        subject = json.loads(Path(a.subject_json).read_text(encoding="utf-8")) if a.subject_json else subject_from_record(a.record)
        if a.dataflik_id:
            subject["dataflik_id"] = a.dataflik_id
        _save(out / "subject.json", subject)
    if not subject:
        raise SystemExit("give --record <uuid> or --subject-json the first time")
    if not a.work:
        raise SystemExit("give --work: the buyer search's house folder (sweep.json, details.json)")
    result = rebuild(Path(a.work), subject, months=a.months, arv=a.arv)
    by_key, by_mail = registry_index(REG_DIR)
    principals = _load(Path(REG_DIR) / "principals.json", {})
    targets = [b for b in result["buyers"] if b.get("target_rank")]
    ranked = []
    for b in targets:
        reg = by_key.get(b["buyer_id"]) or by_key.get(norm(b["buyer_full_name"])) or by_mail.get(mail_key(mail_of(b))) or {}
        last = b.get("last_purchase") or ""
        days = (date.today() - date.fromisoformat(last)).days if re.match(r"\d{4}-\d{2}-\d{2}$", last) else ""
        ranked.append({
            "top": b["target_rank"], "buyer": b["buyer_full_name"], "buyer_id": b["buyer_id"],
            "registry_key": reg.get("buyer_key") or "",
            "saved_uuid": reg.get("saved_uuid") or "", "mail": mail_of(b) or reg.get("mail") or "",
            "score": b.get("score"), "buyer_type": ", ".join(b.get("roles") or []),
            "likely_price": b.get("likely_price"), "local_buys": b.get("purchases_radius") or 0,
            "lifetime_buys": max(b.get("total_investor_purchases") or 0, reg.get("n_buys") or 0),
            "days_since_last": days, "price_med": b.get("median_paid") or 0,
            "in_band": b.get("priced_purchases") or 0, "priced": b.get("priced_purchases") or 0,
            "local": [{"address": h.get("address") or "", "dist": h.get("miles") or 0, "date": h.get("date") or "",
                       "price": h.get("price") or 0, "exit": None, "txn": h.get("role") or "", "beds": "",
                       "sqft": "", "cash": False} for h in b.get("bought_here") or []],
        })
        f = research_file(out, b)
        doc = _load(f, {"entity": b["buyer_full_name"], "decision_makers": [], "verdict": "", "notes": ""})
        known = {re.sub(r"[^a-z]", "", d["name"].lower()) for d in doc["decision_makers"]}
        for dm in (own_decision_maker(b), registry_decision_maker(reg, principals)):
            if dm and re.sub(r"[^a-z]", "", dm["name"].lower()) not in known:
                doc["decision_makers"].append(dm)
                known.add(re.sub(r"[^a-z]", "", dm["name"].lower()))
        if doc["decision_makers"]:
            _save(f, doc)
    _save(out / "ranked.json", {"subject": dict(subject, ask=a.ask or 0, contract=a.contract or 0),
                                "ranked": ranked, "comps": {}})
    _save(out / "buyers.json", targets)
    in_reg = sum(1 for r in ranked if r["registry_key"])
    print("seed: %d buyers to call (%d in the registry, %d with a DataSift record); %d set aside"
          % (len(ranked), in_reg, sum(1 for r in ranked if r["saved_uuid"]), len(result["buyers"]) - len(targets)))


def subject_from_record(uuid: str) -> dict:
    import crm_key_client
    rec = crm_key_client.from_env().get_record(uuid) or {}
    ad = rec.get("address") or {}
    if not ad:
        raise SystemExit("record %s could not be read" % uuid)
    return {"address": ad.get("street") or "", "city": ad.get("city") or "", "state": ad.get("state") or "",
            "zip": str(ad.get("postal_code") or "")[:5], "lat": ad.get("latitude"), "lon": ad.get("longitude"),
            "beds": rec.get("bedrooms"), "baths": rec.get("bathrooms"), "sqft": rec.get("sqft"),
            "year_built": rec.get("year"), "value": rec.get("estimate_value"),
            "dataflik_id": str(rec.get("dataflik_id") or "") or None, "record": uuid}


# --------------------------------------------------------------------------- houses

def owner_is_buyer(rec: dict, buyer: str, people: list[str] | None = None) -> bool:
    """Whether a DataSift record's owner is this buyer (company or person, either name order), or one of the
    people known to run it. A record that names anyone else is usually the house's previous owner: three of one
    buyer's houses named the man who sold them all on one deed."""
    ow = rec.get("owner") or {}
    first, last, company = ow.get("first_name") or "", ow.get("last_name") or "", ow.get("company") or ""
    names = [company, "%s %s" % (first, last), "%s %s" % (last, first)]
    key = norm(buyer)
    if any(n.strip() and norm(n) == key for n in names):
        return True
    want = distinctive(buyer)
    if want and any(n.strip() and want <= distinctive(n) for n in names):
        return True
    if first and last and not company:
        from agents.research.open_research import same_person
        return any(same_person("%s %s" % (first, last), p) for p in people or [])
    return False


def known_people(out: Path, b: dict) -> list[str]:
    return [d["name"] for d in _load(research_file(out, b), {}).get("decision_makers") or []
            if d.get("confidence") in ("high", "medium")]


class _Calls:
    """post_walkthrough.find_record speaks crm.call(); the key client speaks _request()."""

    def __init__(self, crm):
        self.crm = crm

    def call(self, path, method="GET", body=None, headers=None):
        return self.crm._request(path, method=method, body=body,
                                 method_override=(headers or {}).get("x-http-method-override"))


def phase_houses(a, out: Path) -> None:
    import crm_key_client
    import post_walkthrough as pw
    targets = _load(out / "buyers.json", [])
    ranked = _load(out / "ranked.json", {})
    if not targets or not ranked:
        raise SystemExit("run --phase seed first")
    crm = crm_key_client.from_env()
    calls = _Calls(crm)
    cache = _load(out / "houses.json", {})
    slots = {r["top"]: r for r in ranked["ranked"] if r.get("top")}
    members = [r for r in ranked["ranked"] if r.get("same_group_as")]
    have = {(m["same_group_as"], m["saved_uuid"]) for m in members}
    found = 0
    for b in targets:
        slot = slots.get(b["target_rank"])
        if not slot:
            continue
        for h in b.get("bought_here") or []:
            if h.get("role") not in HOLD_ROLES or not h.get("address"):
                continue
            addr = h["address"]
            hit = cache.get(addr)
            if hit is None:
                try:
                    row, why = pw.find_record(calls, addr)
                except Exception as exc:  # noqa: BLE001  (a refused search is a miss, said)
                    row, why = None, "search failed: %s" % str(exc)[:80]
                hit = {"uuid": "", "owner": "", "why": why}
                if row:
                    rec = crm.get_record(row["uuid"]) or {}
                    ow = rec.get("owner") or {}
                    hit = {"uuid": row["uuid"], "owner": ow.get("company") or " ".join(
                        filter(None, [ow.get("first_name"), ow.get("last_name")])), "why": "",
                        "owner_fields": {k: ow.get(k) or "" for k in ("company", "first_name", "last_name")}}
                cache[addr] = hit
                _save(out / "houses.json", cache)
            # judged on every run from the kept owner, so people that research names later count too
            hit["match"] = bool(hit.get("uuid")) and owner_is_buyer({"owner": hit.get("owner_fields") or {}},
                                                                     b["buyer_full_name"], known_people(out, b))
            if hit.get("match") and hit["uuid"] != slot.get("saved_uuid") and (slot["top"], hit["uuid"]) not in have:
                members.append({"same_group_as": slot["top"], "buyer": b["buyer_full_name"],
                                "registry_key": slot.get("registry_key") or "", "saved_uuid": hit["uuid"],
                                "house": addr})
                have.add((slot["top"], hit["uuid"]))
                found += 1
            if not slot.get("saved_uuid") and hit.get("match"):
                slot["saved_uuid"] = hit["uuid"]
    ranked["ranked"] = [r for r in ranked["ranked"] if r.get("top")] + members
    _save(out / "ranked.json", ranked)
    _save(out / "houses.json", cache)
    looked = sum(1 for v in cache.values())
    print("houses: %d held houses looked up, %d in DataSift, %d owned by the buyer; %d records added"
          % (looked, sum(1 for v in cache.values() if v.get("uuid")), sum(1 for v in cache.values() if v.get("match")),
             found))


# ------------------------------------------------------------------------- research

BUYER_SYSTEM = """You find who runs one real estate investor that bought houses around Knoxville, Tennessee, and how to reach them.

Tools: web_search (Google results) and web_fetch (open one page). You have at most %d searches and %d page opens.

Good sources: the company's own website or Facebook page, its BBB profile, Bizapedia or OpenCorporates (Tennessee Secretary of State filings: organizer, registered agent, members or managers), LinkedIn, local news. The Tennessee Secretary of State's own site blocks automated reads; Bizapedia carries the same filing data.

Rules:
- A company with the same name in another state is a different company. Match the city and state of the mailing address you are given.
- A registered agent is often a lawyer or an agent service, not the owner. Call them a registered agent unless a page says they own or manage the company.
- Never guess a person, a phone number or an email. Every person you name needs the URL of a page you opened or saw in search results, and a short quote copied word for word from that page that names them.
- A phone, an email or a website goes in company_contacts only when the page shows it for THIS company, with the URL and a quote.
- If you find nothing, say so: verdict "not found".

End your reply with the line FACT_SHEET_JSON: followed by one JSON object:
{"entity": "...", "verdict": "found" or "not found", "decision_makers": [{"name": "...", "role": "owner, managing member, member, manager, organizer, registered agent or officer", "confidence": "high, medium or low", "address_or_city": "a street address if a page gives one, else the city and state", "evidence": [{"url": "...", "quote": "..."}]}], "company_contacts": [{"type": "phone, email or website", "value": "...", "url": "...", "quote": "..."}], "notes": "one or two sentences"}
High: a page says this person owns, manages or is a member of this company, in this city and state. Medium: an organizer, officer or registered agent whose surname is in the company name or who shares its address. Low: anything else."""

BUYER_NUDGE = ("Now reply with ONLY the line FACT_SHEET_JSON: followed by the one JSON object "
               "(entity, verdict, decision_makers, company_contacts, notes).")


def _norm_text(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9@.]+", " ", (s or "").lower()).split())


def evidence_text(web, url: str) -> str:
    """What the model could have read at `url`: the page it opened, else the search results that listed it."""
    url = (url or "").strip()
    hit = (web.cache.get("page") or {}).get(url) or {}
    parts = [hit.get("text") or ""] if hit.get("ok") else []
    for res in (web.cache.get("search") or {}).values():
        for o in res.get("results") or []:
            if (o.get("link") or "").rstrip("/") == url.rstrip("/"):
                parts.append("%s %s" % (o.get("title") or "", o.get("snippet") or ""))
    return " ".join(parts)


def valid_person(web, entity: str, dm: dict) -> bool:
    """A person counts only when one page the model read names both the person and the company."""
    last = (dm.get("name") or "").replace(",", " ").split()
    last = [t for t in last if len(t) > 2 and t.lower() not in ("jr", "sr", "iii", "llc", "inc")]
    if not last:
        return False
    surname = last[-1].lower()
    company = [t.lower() for t in distinctive(entity)]
    for ev in dm.get("evidence") or []:
        text = _norm_text(evidence_text(web, ev.get("url") or ""))
        if surname in text.split() and (not company or any(t in text.split() for t in company)):
            return True
    return False


def valid_contact(web, c: dict) -> str:
    """The contact's value when its page shows it, else ''."""
    value = (c.get("value") or "").strip()
    text = evidence_text(web, c.get("url") or "")
    kind = (c.get("type") or "").lower()
    if kind == "phone":
        d = digits(value)
        return d if d and d in re.sub(r"\D", "", text) else ""
    if kind == "email":
        return value.lower() if value and value.lower() in text.lower() else ""
    if kind == "website":
        dom = re.sub(r"^https?://(www\.)?", "", value.lower()).split("/")[0]
        return value if dom and (dom in (c.get("url") or "").lower() or dom in text.lower()) else ""
    return ""


def research_one(out: Path, b: dict, reg: dict, model: str) -> dict:
    from agents.research import open_research as orr
    f = research_file(out, b)
    doc = _load(f, {})
    if doc.get("researched"):
        return doc
    web = orr.Web(cache_path=Path(out) / "research" / ".web" / f.name, env=os.environ)
    buys = "; ".join("%s (%s, $%s)" % (h.get("address"), h.get("date"), "{:,}".format(int(h["price"])) if h.get("price") else "no price")
                     for h in (b.get("bought_here") or [])[:4])
    hint = ("The dispo registry names %s as a likely owner (the owner of the house at the company's mailing address); "
            "confirm or correct it.\n" % reg["principal"]) if reg.get("principal") else ""
    ask = ("Buyer: %s (%s)\nMailing address on the county records: %s\nHouses it bought near Knoxville, TN: %s\n%s"
           "Find who owns or runs this buyer and how to reach them." % (
               b["buyer_full_name"], "a company" if b.get("is_buyer_corporation") else "a person",
               mail_of(b) or "not on file", buys or "none listed", hint))
    r = orr.investigate(model, BUYER_SYSTEM % (SEARCHES, PAGES), ask, web, SEARCHES, PAGES,
                        tools=orr.TOOLS[:2], nudge=BUYER_NUDGE)
    web.save()
    fact = r.get("fact") or {}
    kept, dropped = [], []
    for dm in fact.get("decision_makers") or []:
        if not isinstance(dm, dict) or not (dm.get("name") or "").strip():
            continue
        if valid_person(web, b["buyer_full_name"], dm):
            conf = (dm.get("confidence") or "low").lower()
            if "registered agent" in (dm.get("role") or "").lower() and not (distinctive(dm["name"]) & distinctive(b["buyer_full_name"])):
                conf = "low"            # an agent who does not share the company's name is most often its lawyer
            kept.append({"name": dm["name"].strip(), "role": dm.get("role") or "", "confidence": conf,
                         "address_or_city": dm.get("address_or_city") or "", "from": "research",
                         "evidence": dm.get("evidence") or []})
        else:
            dropped.append(dm.get("name"))
    contacts = []
    for c in fact.get("company_contacts") or []:
        if isinstance(c, dict):
            v = valid_contact(web, c)
            if v:
                contacts.append({"type": c.get("type"), "value": v, "url": c.get("url"), "quote": c.get("quote") or ""})
    seen = [c["value"] for c in contacts if c["type"] in ("phone", "email")]
    if kept and seen:
        kept[0]["phones_or_emails_seen"] = seen
    stub = [d for d in doc.get("decision_makers") or [] if d.get("from") != "research"]   # deed, registry, officers
    names = {re.sub(r"[^a-z]", "", d["name"].lower()) for d in stub}
    doc.update({
        "entity": b["buyer_full_name"], "verdict": fact.get("verdict") or ("found" if kept else "not found"),
        "decision_makers": stub + [d for d in kept if re.sub(r"[^a-z]", "", d["name"].lower()) not in names],
        "company_contacts": contacts, "notes": fact.get("notes") or "", "dropped_unsupported": dropped,
        "researched": bool(r.get("searches") or r.get("pages")),
        "research": {"model": r.get("model"), "usd": r.get("usd", 0), "web_usd": round(web.usd, 4),
                     "searches": r.get("searches"), "pages": r.get("pages"), "error": r.get("error") or ""}})
    _save(f, doc)
    return doc


def phase_research(a, out: Path) -> None:
    from agents.research import open_research as orr
    targets = _load(out / "buyers.json", [])
    if not targets:
        raise SystemExit("run --phase seed first")
    by_key, by_mail = registry_index(REG_DIR)
    principals = _load(Path(REG_DIR) / "principals.json", {})
    todo = [b for b in targets if not _load(research_file(out, b), {}).get("researched")]
    if a.limit:
        todo = todo[:a.limit]
    print("research: %d buyers to research, about $%.2f (searches about $0.06 and the model about $0.02 each)"
          % (len(todo), len(todo) * 0.08))
    if not a.commit:
        print("DRY: nothing searched. Re-run with --commit.")
        return
    model = orr._model("AGENTS_RESEARCH_MODEL", orr.DEFAULT_PRIMARY)
    lock, done = threading.Lock(), {"n": 0, "usd": 0.0, "found": 0}

    def one(b):
        reg = by_key.get(b["buyer_id"]) or by_mail.get(mail_key(mail_of(b))) or {}
        reg = dict(reg, principal=(principals.get(reg.get("buyer_key") or "") or {}).get("principal") or "")
        try:
            doc = research_one(out, b, reg, model)
        except Exception as exc:  # noqa: BLE001  (one buyer's failure is said, never stops the rest)
            log.warning("research on %s failed: %s", b["buyer_full_name"], str(exc)[:160])
            return
        with lock:
            done["n"] += 1
            r = doc.get("research") or {}
            done["usd"] += float(r.get("usd") or 0) + float(r.get("web_usd") or 0)
            done["found"] += 1 if any(d["confidence"] in ("high", "medium") for d in doc.get("decision_makers") or []
                                      if d.get("from") == "research") else 0
            if done["n"] % 10 == 0:
                print("  researched %d of %d, $%.2f so far" % (done["n"], len(todo), done["usd"]))

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        list(ex.map(one, todo))
    ledger(out, "open_research", done["usd"], "%d buyers, %d with a named person found" % (done["n"], done["found"]))
    print("research: %d researched, %d with a person named on a page that shows both, $%.2f"
          % (done["n"], done["found"], done["usd"]))


# ------------------------------------------------------------------------- officers

def has_person(doc: dict, reg_principal: str) -> bool:
    return bool(reg_principal) or any((d.get("confidence") or "") in ("high", "medium") for d in doc.get("decision_makers") or [])


def phase_officers(a, out: Path) -> None:
    import enformion_business as eb
    targets = _load(out / "buyers.json", [])
    by_key, by_mail = registry_index(REG_DIR)
    principals = _load(Path(REG_DIR) / "principals.json", {})
    todo = []
    for b in targets:
        if not b.get("is_buyer_corporation"):
            continue
        reg = by_key.get(b["buyer_id"]) or by_mail.get(mail_key(mail_of(b))) or {}
        pr = principals.get(reg.get("buyer_key") or "") or {}
        doc = _load(research_file(out, b), {})
        if has_person(doc, pr.get("principal") or "") or doc.get("officers_tried"):
            continue
        if "businessv2" in str(pr.get("miss") or "").lower():
            continue                      # bought once already in August and came back empty
        todo.append(b)
    print("officers: %d companies still without a named person, up to $%.2f at $0.10 a match (a miss is free)"
          % (len(todo), len(todo) * 0.10))
    if not a.commit or not todo:
        if not a.commit:
            print("DRY: nothing bought. Re-run with --commit.")
        return
    if not eb.is_configured():
        raise SystemExit("ENFORMION_AP_NAME / ENFORMION_AP_PASSWORD are not set")
    billed = found = 0
    for b in todo:
        st = (b.get("buyer_state") or "TN").upper()
        city_state = ", ".join(x for x in (b.get("buyer_city"), st) if x)
        try:
            officers, paid = eb.search_officers(b["buyer_full_name"], city_state)
        except Exception as exc:  # noqa: BLE001
            log.warning("BusinessV2 on %s failed: %s", b["buyer_full_name"], str(exc)[:120])
            continue
        billed += 1 if paid else 0
        ranked = eb.rank_officers(b["buyer_full_name"], officers, st)[:2]
        f = research_file(out, b)
        doc = _load(f, {"entity": b["buyer_full_name"], "decision_makers": []})
        doc["officers_tried"] = True
        for o in ranked:
            doc.setdefault("decision_makers", []).append({
                "name": o["name"], "role": (o.get("title") or "officer").lower(),
                "confidence": eb.principal_confidence(b["buyer_full_name"], o["name"], o.get("title") or ""),
                # Enformion writes "158 Example Way; Sampleton, TN 37999": the trace reads street, city, ST
                "address_or_city": re.sub(r"\s*;\s*", ", ", o.get("address") or ""), "from": "businessv2",
                "evidence": [{"url": "Enformion BusinessV2", "quote": "%s, %s" % (o.get("title") or "officer", o.get("address") or "")}]})
        found += 1 if ranked else 0
        _save(f, doc)
    ledger(out, "enformion_businessv2", billed * 0.10, "%d searched, %d billed, %d with an in-state officer"
           % (len(todo), billed, found))
    print("officers: %d searched, %d billed ($%.2f), %d with an in-state officer" % (len(todo), billed, billed * 0.10, found))


# ------------------------------------------------------------------------- contacts

def trim(out: Path) -> None:
    """Fewer, better pairs for the name check: no number the CRM already marked bad, at most MAX_NUMBERS a buyer
    (CORRECT first, then the registry's tier, then an East Tennessee area code, then a mobile), at most MAX_DMS
    named people, and no foil that is the buyer itself (a person buyer's own record names them)."""
    dossier = _load(out / "dossier.json", [])
    scores = _load(Path(REG_DIR) / "phone_scores.json", {})
    ids = {r["top"]: r.get("buyer_id") or r.get("buyer") for r in (_load(out / "ranked.json", {}).get("ranked") or [])
           if r.get("top")}
    for d in dossier:
        # a line the buyer publishes itself (its site, its BBB page) is checked and listed like any other
        have = {p["number"] for p in d.get("phones") or []}
        doc = _load(research_file(out, {"buyer_id": ids.get(d["rank"]) or d["buyer"]}), {})
        for c in doc.get("company_contacts") or []:
            n = digits(c.get("value")) if c.get("type") == "phone" else ""
            if n and n not in have:
                d.setdefault("phones", []).append({"number": n, "type": "Published line", "published": True,
                                                   "sources": ["published (%s)" % (c.get("url") or "research")],
                                                   "status": "", "traced_for": "", "via": d["buyer"]})
                have.add(n)
        good = [p for p in d.get("phones") or [] if (p.get("status") or "").upper() not in BAD_STATUS and not p.get("dnc")]
        good.sort(key=lambda p: ((p.get("status") or "").upper() != "CORRECT", not p.get("published"),
                                 TIER_RANK.get((scores.get(p["number"]) or {}).get("tier"), 4),
                                 p["number"][:3] not in ("865", "423"),
                                 "mobile" not in (p.get("type") or "").lower()))
        d["phones_all"] = len(d.get("phones") or [])
        d["phones"] = good[:MAX_NUMBERS]
        n = 0
        for dm in d.get("decision_makers") or []:
            if dm.get("confidence") in ("high", "medium"):
                n += 1
                if n > MAX_DMS:
                    dm["confidence"] = "low"
        owner = norm(d.get("crm_owner") or "")
        if owner and (owner == norm(d["buyer"]) or any(set(norm(x["name"]).split()) & set(owner.split())
                                                        for x in d.get("decision_makers") or [])):
            d["crm_owner"] = ""
    _save(out / "dossier.json", dossier)


def trim_traced(out: Path) -> None:
    traced = _load(out / "traced.json", {})
    for v in traced.values():
        v["phones"] = (v.get("phones") or [])[:MAX_NUMBERS]
    _save(out / "traced.json", traced)


def phase_contacts(a, out: Path) -> None:
    import deal_buyer_dp as dp
    ns = argparse.Namespace(commit=a.commit, slug=a.slug)
    dp.phase_crm(ns, out)
    dp.phase_dossier(ns, out)
    trim(out)
    dp.phase_trace(ns, out)
    if a.commit:
        trim_traced(out)
    dp.phase_verify(ns, out)
    if a.commit:
        dp.phase_export(ns, out)
        finish_workbook(out, a.slug)


def finish_workbook(out: Path, slug: str) -> None:
    """deal_buyer_dp's workbook, fitted to a full buyer list: the first sheet named for what it is, each buyer's
    price guess beside their score, and every email and published line research found."""
    from openpyxl import load_workbook
    path = out / ("Buyers_%s.xlsx" % slug)
    if not path.is_file():
        return
    wb = load_workbook(path)
    ws = wb.worksheets[0]
    ws.title = "Buyers to call"
    rk = _load(out / "ranked.json", {})
    ranked = {r["top"]: r for r in (rk.get("ranked") or []) if r.get("top")}
    where = ((rk.get("subject") or {}).get("address") or slug).split(",")[0]
    ws["A1"].value = "%s: %d buyers to call, who decides at each, and the numbers checked against them" % (
        where, len(ranked))
    ws.insert_cols(4)
    ws.cell(row=3, column=4, value="Would pay (a guess)")
    for row in ws.iter_rows(min_row=4):
        rank = row[0].value
        guess = (ranked.get(rank) or {}).get("likely_price") if isinstance(rank, int) else None
        row[3].value = guess
        row[3].number_format = '"$"#,##0'
    # deal_buyer_match's "why" reads a 3-mile deal ("buys within ~3 mi", "in the ask band"); this list came from the
    # buyer search, so the line is rewritten from what the search saw, and the role codes become words.
    ws.cell(row=3, column=5, value="How they buy")
    words = {"holder": "holds", "flipped": "flips", "entity buy": "company buy"}
    for row in ws.iter_rows(min_row=4):
        r = ranked.get(row[0].value) if isinstance(row[0].value, int) else None
        if not r:
            continue
        row[4].value = ", ".join(words.get(x.strip(), x.strip()) for x in (r.get("buyer_type") or "").split(",") if x.strip())
        local = sorted(r.get("local") or [], key=lambda h: h.get("date") or "", reverse=True)
        last = local[0] if local else {}
        row[11].value = "Bought %d like this nearby%s; %s houses in all%s%s" % (
            r.get("local_buys") or 0,
            (" (latest %s, %s, %s)" % (last.get("date") or "", (last.get("address") or "").split(",")[0],
                                       ("$%s" % "{:,}".format(int(last["price"]))) if last.get("price") else "no price"))
            if last else "",
            r.get("lifetime_buys") or 0,
            ("; pays a median $%s" % "{:,}".format(int(r["price_med"]))) if r.get("price_med") else "",
            ("; would pay about $%s for this one (a guess)" % "{:,}".format(int(r["likely_price"])))
            if r.get("likely_price") else "")
    for col, w in zip("ABCDEFGHIJKL", (5, 30, 8, 12, 16, 24, 22, 10, 18, 11, 7, 70)):
        ws.column_dimensions[col].width = w
    if "Comps" in wb.sheetnames and wb["Comps"].max_row <= 3:
        del wb["Comps"]                      # the buyer search priced no comps; an empty sheet only confuses
    ex = wb.create_sheet("Emails and sites")
    ex.append(["Emails, websites and published lines for each buyer"])
    ex.append([])
    ex.append(["#", "Buyer", "Kind", "Value", "Where it was found"])
    dossier = {d["rank"]: d for d in _load(out / "dossier.json", [])}
    for rank in sorted(ranked):
        d = dossier.get(rank) or {}
        for e in d.get("crm_emails") or []:
            ex.append([rank, ranked[rank]["buyer"], "email", e, "DataSift record"])
        doc = _load(research_file(out, ranked[rank]), {})
        for c in doc.get("company_contacts") or []:
            ex.append([rank, ranked[rank]["buyer"], c.get("type"), c.get("value"), c.get("url")])
    for col, w in zip("ABCDE", (5, 34, 10, 34, 60)):
        ex.column_dimensions[col].width = w
    wb.save(path)


def ledger(out: Path, key: str, usd: float, note: str) -> None:
    p = Path(out) / "ledger.json"
    led = _load(p, [])
    led.append({"when": date.today().isoformat(), "what": key, "usd": round(usd, 4), "note": note})
    _save(p, led)


REG_DIR = Path(".")


def main(argv=None) -> int:
    global REG_DIR
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--phase", required=True, choices=["seed", "houses", "research", "officers", "contacts"])
    ap.add_argument("--work", help="seed: the buyer search's house folder (sweep.json, details.json)")
    ap.add_argument("--record", help="seed: the house's DataSift record uuid")
    ap.add_argument("--subject-json", help="seed: the house as JSON instead of a record")
    ap.add_argument("--dataflik-id", help="seed: the house's SiftMap id when the record does not carry it")
    ap.add_argument("--arv", type=float, help="seed: price the guesses' flips against this ARV")
    ap.add_argument("--ask", type=float, help="seed: the asking price, for the workbook title")
    ap.add_argument("--contract", type=float, help="seed: the contract price, for the workbook title")
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--limit", type=int, default=0, help="research: at most this many buyers")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--env", help="the .env to load keys from (default: this checkout's)")
    ap.add_argument("--out", help="output folder (default output/deal_buyers/<slug>)")
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(Path(a.env) if a.env else None)
    REG_DIR = Path(os.environ.get("DISPO_REGISTRY_DIR") or ROOT / "output" / "dispo_buyers")
    os.environ.setdefault("DISPO_REGISTRY_DIR", str(REG_DIR))
    out = Path(a.out) if a.out else ROOT / "output" / "deal_buyers" / a.slug
    out.mkdir(parents=True, exist_ok=True)
    {"seed": phase_seed, "houses": phase_houses, "research": phase_research, "officers": phase_officers,
     "contacts": phase_contacts}[a.phase](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
