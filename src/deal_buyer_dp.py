"""Deep prospect the top buyers for one deal: who signs, and which number reaches them.

Reads `deal_buyer_match.py`'s ranked.json, the dispo registry's phones, and the
research layer a human or agent writes to `research/<slug>.json` (every claim a
URL plus a verbatim quote). The module never invents a decision maker.

Phases (DRY by default; --commit to spend):
  dossier    merge top N + registry phones + research (free)
  trace      SmartSkip on every named decision maker with a verified home
             street (~$0.15/hit); never a name plus a city (see _trace_targets)
  verify     Trestle Real Contact on every (decision maker, phone) pair: name
             match, contact grade, activity score, litigator flag
  export     workbook + dial CSV

    python src/deal_buyer_dp.py --slug 5123-odell --phase dossier
    python src/deal_buyer_dp.py --slug 5123-odell --phase trace --commit
    python src/deal_buyer_dp.py --slug 5123-odell --phase verify --commit
    python src/deal_buyer_dp.py --slug 5123-odell --phase export

WHY REAL CONTACT AND NOT phone_intel: phone_intel says a line is live, not whose
it is. The registry's numbers for an entity buyer sit on the COMPANY record with
no person attached (GDP Properties carries 504 and 201 area codes), so the only
way to know the decision maker answers is a name match. Reverse Phone is not
licensed on this account (403); Real Contact is, verified 2026-10-27.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import config  # noqa: F401  loads .env
except Exception:
    pass

from deal_buyer_match import REG_DIR, norm_name  # noqa: E402  (one registry folder, one name key)

log = logging.getLogger("deal_buyer_dp")

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills/deep-prospecting-v5/scripts"
REAL_CONTACT = "https://api.trestleiq.com/1.1/real_contact"
SMARTSKIP_PER_HIT = 0.15
TIERS = [(81, "Dial First"), (61, "Dial Second"), (41, "Dial Third"), (21, "Dial Fourth"), (0, "Drop")]
RANK_CONF = {"high": 0, "medium": 1, "low": 2, "": 3}
LOCAL_AC = {"865", "423"}   # East Tennessee


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def digits(s) -> str:
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""


def fmt_phone(n: str) -> str:
    return "(%s) %s-%s" % (n[:3], n[3:6], n[6:]) if len(n) == 10 else n


def tier_of(score) -> str:
    if score is None:
        return "Unscored"
    return next(t for floor, t in TIERS if score >= floor)


def ledger(out: Path, key: str, amount: float, note: str) -> None:
    led = _load(out / "ledger.json", [])
    led.append({"when": datetime.now().isoformat(timespec="seconds"), "key": key,
                "amount": round(amount, 2), "note": note})
    _save(out / "ledger.json", led)


def split_name(full: str) -> tuple:
    """(first, last) from a display name; drops suffixes and middle initials."""
    toks = [t for t in re.sub(r"[^A-Za-z' -]", " ", full or "").split()
            if t.upper().rstrip(".") not in ("JR", "SR", "II", "III", "IV")]
    if len(toks) < 2:
        return (toks[0] if toks else "", "")
    return toks[0], toks[-1]


# -------------------------------------------------------------- dossier ----

def _research(out: Path) -> list:
    docs = []
    for f in sorted((out / "research").glob("*.json")):
        d = _load(f, None)
        if isinstance(d, dict):
            d["_slug"] = f.stem
            docs.append(d)
    return docs


def _names_of(row: dict, reg: dict) -> set:
    names = {norm_name(row.get("buyer") or ""), norm_name(reg.get("name") or "")}
    names |= {norm_name(x) for x in reg.get("aliases") or []}
    names.discard("")
    return names


def _members(slot: dict, ranked: list) -> list:
    """The slot's own row first, then every ranked row folded under it."""
    return [slot] + [r for r in ranked if r.get("same_group_as") == slot["top"]]


def _dm(dm: dict, source: str, via: str = "") -> dict:
    return {"name": dm["name"].strip(), "role": dm.get("role") or "",
            "confidence": (dm.get("confidence") or "").lower(), "source": source, "via": via,
            "address": dm.get("address_or_city") or "", "evidence": dm.get("evidence") or [],
            "seen": dm.get("phones_or_emails_seen") or []}


def _read_record(crm, uuid: str) -> dict:
    """One record over the Open API key. `crm` is the agents' budgeted client
    (record()) or the standalone key client (get_record()); a read that fails
    returns {} and is counted, never mistaken for an empty record."""
    try:
        if hasattr(crm, "record"):
            return crm.record(uuid) or {}
        return crm.get_record(uuid) or {}
    except Exception as exc:  # noqa: BLE001  (counted as unreadable by the caller)
        log.warning("record %s unreadable: %s", uuid, str(exc)[:160])
        return {}


def phase_crm(a, out: Path, crm=None) -> None:
    """Read the live CRM record of every slot and group member (read only).

    Over the Open API key alone: it used to go through the texting app's CRM
    client, which prefers the Deal Room checkout and does not exist on the
    agents box."""
    ranked = _load(out / "ranked.json", None)
    if not ranked:
        raise SystemExit("run deal_buyer_match.py --phase rank first")
    if crm is None:
        import crm_key_client
        crm = crm_key_client.from_env()
    rows = [r for r in ranked["ranked"] if r.get("top") or r.get("same_group_as")]
    recs = _load(out / "crm_records.json", {})
    miss = 0
    for r in rows:
        u = r.get("saved_uuid") or ""
        if not u or u in recs:
            continue
        rec = _read_record(crm, u)
        if rec:
            recs[u] = rec
        else:
            miss += 1
    _save(out / "crm_records.json", recs)
    print("crm records held %d, unreadable %d" % (len(recs), miss))


def phase_dossier(a, out: Path) -> None:
    ranked = _load(out / "ranked.json", None)
    if not ranked:
        raise SystemExit("run deal_buyer_match.py --phase rank first")
    allr = ranked["ranked"]
    top = sorted((r for r in allr if r.get("top")), key=lambda r: r["top"])
    reg = {b["buyer_key"]: b for b in _load(REG_DIR / "registry.json", [])}
    prof = {p["buyer_key"]: p for p in _load(REG_DIR / "buyer_profiles.json", [])}
    phones = _load(REG_DIR / "phones.json", {})
    crm_recs = _load(out / "crm_records.json", {})
    groups = {g["name"]: g for g in (_load(out / "groups.json", {}) or {}).get("groups", [])}
    docs = _research(out)

    dossier = []
    for b in top:
        mem = _members(b, allr)
        g = groups.get(b.get("group") or "", {})
        dms, docs_hit, nums, foils, emails = [], [], {}, [], []
        for dm in g.get("decision_makers") or []:
            dms.append(_dm(dm, "groups.json", g["name"]))
        for m in mem:
            rk = m.get("registry_key") or ""
            r, p, ph = reg.get(rk, {}), prof.get(rk, {}), phones.get(rk, {})
            names = _names_of(m, r)
            # "KNOX DEVELOPMENT INC (TN: Knox Development, Inc.)": researchers
            # annotate the entity, so the parenthetical is dropped before the join.
            res = next((d for d in docs if norm_name(re.sub(r"\(.*?\)", "", d.get("entity") or ""))
                        in names), {})
            if res and any(x["_slug"] == res["_slug"] for x in docs_hit):
                res = {}
            if res:
                docs_hit.append(res)
            for dm in res.get("decision_makers") or []:
                if dm.get("name") and dm["name"].strip().lower() != "not found":
                    dms.append(_dm(dm, "research/" + res["_slug"], m["buyer"]))
            verdict = (res.get("verdict") or "").lower()
            pr = p.get("principal") or ""
            if pr and verdict not in ("refuted", "corrected") and not any(
                    split_name(pr)[1].lower() == split_name(d["name"])[1].lower() for d in dms):
                dms.append(_dm({"name": pr, "role": "owner of the LLC's mailing address",
                                "confidence": "medium" if not res else "low",
                                "address_or_city": r.get("mail") or ""},
                               p.get("principal_source") or "registry", m["buyer"]))
            for q in ph.get("phones") or []:
                n = digits(q.get("number"))
                if n:
                    src = q.get("source") or "smartskip"
                    nums.setdefault(n, {"number": n, "type": (q.get("type") or "").title(),
                                        "sources": [src], "status": q.get("status") or "",
                                        "traced_for": ph.get("person") if "smartskip" in src else "",
                                        "via": m["buyer"]})
            # The live CRM record outranks the August registry snapshot: the
            # blast engine writes CORRECT on engagement, WRONG / DNC on a miss.
            rec = crm_recs.get(m.get("saved_uuid") or "") or {}
            ow = rec.get("owner") or {}
            for q in ow.get("phones") or []:
                n = digits(q.get("number"))
                if not n:
                    continue
                e = nums.setdefault(n, {"number": n, "type": (q.get("type") or "").title(),
                                        "sources": ["datasift-native"], "traced_for": "",
                                        "via": m["buyer"]})
                e["status"] = (q.get("status") or "").upper()
                e["dnc"] = bool(q.get("doNotCall") or q.get("do_not_call"))
            # A record whose owner is now a PERSON is usually a flip that sold
            # to a homebuyer: its phones were harvested for the buyer but may be
            # the homeowner's. That name is tested too, as a foil.
            person = " ".join(filter(None, [ow.get("first_name"), ow.get("last_name")])).strip()
            if person and not ow.get("company"):
                foils.append(person)
            emails += [e.get("email") if isinstance(e, dict) else e for e in ow.get("emails") or []]
        seen_dm, uniq = set(), []
        for d in sorted(dms, key=lambda d: RANK_CONF.get(d["confidence"], 3)):
            k = re.sub(r"[^a-z]", "", d["name"].lower())
            if k not in seen_dm:
                seen_dm.add(k)
                uniq.append(d)
        dossier.append({
            "rank": b["top"], "buyer": b["buyer"], "group": b.get("group") or "",
            "group_why": g.get("why") or "", "members": [m["buyer"] for m in mem[1:]],
            "registry_key": b.get("registry_key") or "", "score": b["score"],
            "mail": b.get("mail") or "", "saved_uuid": b.get("saved_uuid") or "",
            "research_slug": ", ".join(d["_slug"] for d in docs_hit),
            "research_verdict": ", ".join(filter(None, (d.get("verdict") or "" for d in docs_hit))),
            "research_notes": " | ".join(filter(None, (d.get("notes") or "" for d in docs_hit))),
            "tn_sos": [d.get("tn_sos") or {} for d in docs_hit],
            "related": [x for d in docs_hit for x in d.get("related_entities") or []],
            "other_people": [x for d in docs_hit for x in d.get("other_people") or []],
            "decision_makers": uniq, "phones": list(nums.values()),
            "crm_owner": foils[0] if foils else "", "crm_emails": [e for e in emails if e],
        })
    _save(out / "dossier.json", dossier)
    for d in dossier:
        dm = d["decision_makers"][0] if d["decision_makers"] else {}
        print("%2d %-34s DM %-28s %-6s %-22s phones %2d  research %s"
              % (d["rank"], d["buyer"][:34], (dm.get("name") or "NONE")[:28],
                 dm.get("confidence", ""), (dm.get("source") or "")[:22], len(d["phones"]),
                 d["research_slug"] or "MISSING"))


# ---------------------------------------------------------------- trace ----

def _street_of(addr: str) -> tuple:
    """(street, city, state, zip) for a HOME street, else empty.

    An office or suite is refused: SmartSkip matches a person to where they
    live, and a person traced at their company's suite returns nothing or,
    worse, a different person who shares the building.
    """
    if re.search(r"\boffice\b|\bste\b|\bsuite\b|\bpo box\b", addr or "", re.I):
        return ("", "", "", "")
    m = re.search(r"(\d+ [A-Za-z0-9 .'-]+?),\s*([A-Za-z .'-]+?),\s*([A-Z]{2})\s*(\d{5})?\b", addr or "")
    if not m:
        return ("", "", "", "")
    return (m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4) or "")


def _ss_module(out: Path):
    os.environ["SKIPTRACE_RUN_DIR"] = str(out / ".skiptrace_run")
    sys.path.insert(0, str(SKILL_SCRIPTS))
    import smartskip_trace as ss  # noqa: E402
    import parse_smartskip as ps  # noqa: E402
    return ss, ps


def _trace_targets(dossier: list, overrides: dict) -> tuple:
    """DMs worth tracing: high/medium confidence, a HOME street, not yet traced.

    There is deliberately no name-plus-city fallback. Knox County holds several
    people sharing a principal's common name; a city-anchored person search
    returns whichever ranks first, and Real Contact then confirms the number
    belongs to A person of that name, a false positive the verify step cannot see. A decision maker with
    no verified home is checked only against numbers already tied to the
    company, and listed in `skipped` so the gap is visible.
    """
    ss_rows, skipped = [], []
    for d in dossier:
        for dm in _dms(d):
            first, last = split_name(dm["name"])
            if not first or not last:
                continue
            already = any(split_name(p.get("traced_for") or "")[1].lower() == last.lower()
                          for p in d["phones"])
            if already:
                continue
            addr = overrides.get(dm["name"]) or dm.get("address") or ""
            street, city, st, z = _street_of(addr)
            if street:
                ss_rows.append({"First Name": first, "Last Name": last, "Mailing Address": street,
                                "Mailing City": city, "Mailing State": st, "Mailing Zip": z,
                                "_rank": d["rank"], "_name": dm["name"]})
            else:
                skipped.append((d["rank"], dm["name"], "no verified home street"))
    return ss_rows, skipped


def _dms(d: dict) -> list:
    return [dm for dm in d["decision_makers"] if dm["confidence"] in ("high", "medium")][:3]


def phase_trace(a, out: Path) -> None:
    dossier = _load(out / "dossier.json", None)
    if not dossier:
        raise SystemExit("run --phase dossier first")
    # trace.json: {"Decision Maker Name": "street, city, ST zip"} where research
    # established a home the free-text address field does not state cleanly.
    ss_rows, skipped = _trace_targets(dossier, _load(out / "trace.json", {}))
    traced = _load(out / "traced.json", {})
    print("SmartSkip (home street known): %d | not traced: %d" % (len(ss_rows), len(skipped)))
    for r in ss_rows:
        print("  SS   #%-2d %-26s %s, %s %s" % (r["_rank"], r["_name"], r["Mailing Address"],
                                               r["Mailing City"], r["Mailing State"]))
    for rank, name, why in skipped:
        print("  SKIP #%-2d %-26s %s" % (rank, name, why))
    if not a.commit:
        print("DRY: nothing billed. Max spend about $%.2f. Re-run with --commit."
              % (len(ss_rows) * SMARTSKIP_PER_HIT))
        return

    if ss_rows:
        cols = ["First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        w.writerows(ss_rows)
        csv_path = out / "ss_input.csv"
        csv_path.write_text(buf.getvalue(), encoding="utf-8")
        ss, ps = _ss_module(out)
        sess = ss.Session()
        st = _load(out / "smartskip_state.json", {})
        if st.get("csv") != buf.getvalue() or not st.get("bulkSkipId"):
            entry = ss.submit(sess, str(csv_path), {})
            st = {"csv": buf.getvalue(), "bulkSkipId": entry["bulkSkipId"],
                  "entities": entry.get("entities"), "paid": False}
            _save(out / "smartskip_state.json", st)
        if not st.get("paid"):
            if not ss.pay(sess, st["bulkSkipId"]):
                raise SystemExit("SmartSkip payment did not go through; finish it in the app and re-run")
            st["paid"] = True
            _save(out / "smartskip_state.json", st)
        t0 = time.time()
        while True:
            items = ss.fetch_status(sess, st["bulkSkipId"])
            s = ((items[0].get("status") if items else "") or "").lower()
            if s == "completed":
                break
            if s in ("error", "failed") or time.time() - t0 > 900:
                raise SystemExit("SmartSkip order %s ended %s; the order is paid and saved" % (st["bulkSkipId"], s))
            time.sleep(15)
        ss.download(sess, st["bulkSkipId"], str(out / "ss_vertical.csv"), "vertical")
        parsed = {re.sub(r"[^a-z]", "", (p.get("input_name") or "").lower()): p
                  for p in ps.parse(str(out / "ss_vertical.csv"))}
        hits = 0
        for row in ss_rows:
            k = re.sub(r"[^a-z]", "", (row["First Name"] + row["Last Name"]).lower())
            p = parsed.get(k) or next((v for kk, v in parsed.items()
                                       if kk.startswith(k[:4]) and kk.endswith(k[-4:])), {})
            got = [digits(x.get("number")) for x in p.get("subject_phones") or []]
            got = [g for g in got if g]
            hits += 1 if p.get("has_results") else 0
            traced[row["_name"]] = {"rank": row["_rank"], "via": "smartskip", "phones": got,
                                    "deceased_flag": p.get("deceased")}
        ledger(out, "smartskip", hits * SMARTSKIP_PER_HIT, "%d hits of %d" % (hits, len(ss_rows)))

    _save(out / "traced.json", traced)
    for k, v in traced.items():
        print("  %-28s via %-9s phones %d" % (k, v["via"], len(v["phones"])))


# --------------------------------------------------------------- verify ----

def _real_contact(key: str, name: str, phone: str, state: str) -> dict:
    h = {"x-api-key": key, "Accept": "application/json"}
    params = {"name": name, "phone": phone, "add_ons": "litigator_checks"}
    if state:
        params["address.state_code"] = state
    for attempt in range(4):
        try:
            r = requests.get(REAL_CONTACT, headers=h, params=params, timeout=20)
        except requests.RequestException as e:
            if attempt == 3:
                return {"error": str(e)[:200]}
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        return {"error": "HTTP %s %s" % (r.status_code, r.text[:200])}
    return {"error": "rate limited"}


def _pairs(dossier: list, traced: dict) -> list:
    pairs = []
    for d in dossier:
        dms = _dms(d)
        company = {p["number"] for p in d["phones"]}
        # A number traced at one person's home is tested against that person
        # only; numbers on the company record are tested against everyone.
        for dm in dms:
            own = set((traced.get(dm["name"]) or {}).get("phones") or [])
            own |= {digits(s) for s in dm.get("seen") or [] if digits(s)}
            for n in sorted(company | own):
                pairs.append((d["rank"], dm["name"], n))
        if not dms:
            # No named human: still score the line so the caller knows it is live.
            for n in sorted(company):
                pairs.append((d["rank"], "", n))
        if d.get("crm_owner"):
            for n in sorted(p["number"] for p in d["phones"]):
                pairs.append((d["rank"], "FOIL:" + d["crm_owner"], n))
    return pairs


def phase_verify(a, out: Path) -> None:
    dossier = _load(out / "dossier.json", None)
    if not dossier:
        raise SystemExit("run --phase dossier first")
    traced = _load(out / "traced.json", {})
    cache = _load(out / "verify.json", {})
    pairs = _pairs(dossier, traced)
    todo = [p for p in pairs if "%s|%s" % (p[1], p[2]) not in cache]
    print("pairs %d, cached %d, to query %d" % (len(pairs), len(pairs) - len(todo), len(todo)))
    if not a.commit:
        print("DRY: nothing queried. Re-run with --commit.")
        return
    key = os.getenv("TRESTLE_PAID_API_KEY") or os.getenv("TRESTLE_API_KEY")
    if not key:
        raise SystemExit("no Trestle key in the environment")
    errors = 0
    for i, (rank, name, n) in enumerate(todo, 1):
        # With no named person, the company name still returns the activity
        # score and line type; it simply never name-matches.
        if name:
            # "Rajendra (Raj) Adhikari" -> "Rajendra Adhikari": the matcher
            # wants a plain first and last name, not a researcher's annotation.
            who = " ".join(split_name(name.replace("FOIL:", "")))
        else:
            who = next(d["buyer"] for d in dossier if d["rank"] == rank)
        res = _real_contact(key, who, n, "")
        if res.get("error"):
            errors += 1
        cache["%s|%s" % (name, n)] = {"rank": rank, "name": name, "number": n, **res}
        if i % 20 == 0:
            _save(out / "verify.json", cache)
            log.info("verified %d/%d", i, len(todo))
    _save(out / "verify.json", cache)
    ledger(out, "trestle_real_contact", 0.0, "%d queries, %d errors (rate not published here)"
           % (len(todo), errors))
    if todo and errors == len(todo):
        raise SystemExit("GATE FAILED: every Real Contact call errored; check the key")
    print("verified %d, errors %d" % (len(todo), errors))


# --------------------------------------------------------------- export ----

def _phone_rows(dossier: list, traced: dict, cache: dict) -> list:
    rows = []
    for d in dossier:
        src = {p["number"]: p for p in d["phones"]}
        dms = _dms(d)
        per_num, foil_hit = {}, set()
        for v in cache.values():
            if v.get("rank") != d["rank"] or v.get("error"):
                continue
            n = v["number"]
            if (v.get("name") or "").startswith("FOIL:"):
                if v.get("phone.name_match"):
                    foil_hit.add(n)
                if n in per_num:
                    continue
            cur = per_num.get(n)
            match = bool(v.get("phone.name_match")) and not (v.get("name") or "").startswith("FOIL:")
            if cur is None or (match and not cur["match"]):
                per_num[n] = {"match": match, "matched_to": v["name"] if match else "",
                              "grade": v.get("phone.contact_grade") or "",
                              "activity": v.get("phone.activity_score"),
                              "line": v.get("phone.linetype") or "",
                              "valid": v.get("phone.is_valid"),
                              "litigator": ((v.get("add_ons") or {}).get("litigator_checks") or {})
                              .get("phone.is_litigator_risk")}
        for n, v in per_num.items():
            sources = list((src.get(n) or {}).get("sources") or [])
            for dm in dms:
                t = traced.get(dm["name"]) or {}
                if n in (t.get("phones") or []):
                    sources.append(t["via"] + " (" + dm["name"] + ")")
            crm = src.get(n) or {}
            tier = tier_of(v["activity"])
            if n in foil_hit:
                tier = "Not the buyer (%s)" % d["crm_owner"]
            elif crm.get("status") in ("WRONG", "DNC", "CORRECT_DNC", "WRONG_DNC", "DEAD") or crm.get("dnc"):
                tier = "Skip: CRM says %s" % (crm.get("status") or "DNC")
            rows.append({"rank": d["rank"], "buyer": d["buyer"],
                         "decision_maker": v["matched_to"] or (dms[0]["name"] if dms else ""),
                         "dm_match": "Yes" if v["match"] else ("No" if dms else "No name to test"),
                         "phone": fmt_phone(n), "number": n, "line": v["line"],
                         "activity": v["activity"], "tier": tier,
                         "crm_status": crm.get("status") or "",
                         "grade": v["grade"], "valid": v["valid"],
                         "litigator": "YES" if v["litigator"] else "",
                         "sources": ", ".join(dict.fromkeys(sources)) or "research"})
    order = {t: i for i, (_, t) in enumerate(TIERS)}
    # Ties go to a local area code: these buyers live around Knoxville, and an
    # old out-of-state cell that still scores 100 is the second call, not the first.
    rows.sort(key=lambda r: (r["rank"], r["dm_match"] != "Yes", r["litigator"] == "YES",
                             r["crm_status"] != "CORRECT", order.get(r["tier"], 9),
                             -(r["activity"] or 0), r["number"][:3] not in LOCAL_AC))
    return rows


def phase_export(a, out: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    dossier = _load(out / "dossier.json", [])
    ranked = _load(out / "ranked.json", {})
    traced = _load(out / "traced.json", {})
    cache = _load(out / "verify.json", {})
    rows = _phone_rows(dossier, traced, cache)
    by_rank = {}
    for r in rows:
        by_rank.setdefault(r["rank"], []).append(r)
    top = {r["top"]: r for r in ranked.get("ranked", []) if r.get("top")}
    subj = ranked.get("subject", {})
    # The titles name THIS deal. They carried the first deal's street (5123 Odell)
    # as a literal, so every later workbook was headed with the wrong house.
    where = (subj.get("address") or getattr(a, "slug", "") or "this deal").split(",")[0]

    navy, line_c = "0A1130", "D7DCE5"
    head_font = Font(bold=True, color=navy)
    head_fill = PatternFill("solid", fgColor="F3F5F9")
    thin = Side(style="thin", color=line_c)
    wrap = Alignment(wrap_text=True, vertical="top")

    def sheet(ws, title, headers, data, widths):
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=13, color=navy)
        ws.append([])
        ws.append(headers)
        for c in ws[3]:
            c.font, c.fill, c.border = head_font, head_fill, Border(bottom=thin)
            c.alignment = Alignment(wrap_text=True, vertical="bottom")
        for row in data:
            ws.append(row)
        for row in ws.iter_rows(min_row=4):
            for c in row:
                c.alignment = wrap
                c.border = Border(bottom=Side(style="hair", color=line_c))
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "B4"

    wb = Workbook()
    ws = wb.active
    ws.title = "Top 10 Buyers"
    data = []
    for d in dossier:
        t = top.get(d["rank"], {})
        best = next((r for r in by_rank.get(d["rank"], []) if r["dm_match"] == "Yes"
                     and r["litigator"] != "YES" and r["tier"] in ("Dial First", "Dial Second")), None)
        dm = d["decision_makers"][0] if d["decision_makers"] else {}
        if best:
            dm = next((x for x in d["decision_makers"] if x["name"] == best["decision_maker"]), dm)
            number, tier, grade = best["phone"], best["tier"], best["grade"]
        else:
            # No personal number verified: the published company line, which
            # research found on the buyer's own site or BBB, is the way in.
            line = next((s for x in d["decision_makers"] for s in x.get("seen") or []
                         if digits(s)), "")
            nick = re.search(r"\((\w+)\)", dm.get("name") or "")
            first = nick.group(1) if nick else split_name(dm.get("name") or "")[0]
            number = ("%s company line, ask for %s" % (fmt_phone(digits(line)), first)
                      if line else "No valid number")
            tier, grade = "", ""
        label = d["buyer"] + ((" + " + ", ".join(d["members"])) if d.get("members") else "")
        local = t.get("local") or []
        near = min(local, key=lambda o: o["dist"]) if local else {}
        why = ("%d buys within ~3 mi (closest %s at %.2f mi, %s for $%s); %d lifetime; last buy %s days ago; "
               "pays $%s median, %d of %d priced buys in the ask band"
               % (t.get("local_buys", 0), near.get("address", "").split(",")[0], near.get("dist", 0),
                  near.get("date", ""), "{:,.0f}".format(near.get("price") or 0),
                  t.get("lifetime_buys", 0), t.get("days_since_last", ""),
                  "{:,.0f}".format(t.get("price_med") or 0), t.get("in_band", 0), t.get("priced", 0)))
        data.append([d["rank"], label, t.get("score"), t.get("buyer_type") or "",
                     dm.get("name") or "Not verified", dm.get("role") or "", dm.get("confidence") or "",
                     number, tier, grade, why])
    sheet(ws, "%s: top %d buyers (ask $%s, contract $%s)"
          % (where, len(dossier), "{:,.0f}".format(subj.get("ask") or 0), "{:,.0f}".format(subj.get("contract") or 0)),
          ["#", "Buyer", "Fit score", "Model", "Decision maker", "Role", "Confidence",
           "Best verified number", "Tier", "Grade", "Why this buyer"],
          data, [4, 30, 8, 14, 24, 22, 10, 16, 11, 7, 70])

    ws = wb.create_sheet("Dial Sheet")
    sheet(ws, "Every number, scored and name-checked against the decision maker",
          ["#", "Buyer", "Decision maker", "DM match", "Phone", "Line", "Activity", "Tier",
           "Grade", "Litigator", "CRM status", "Source"],
          [[r["rank"], r["buyer"], r["decision_maker"], r["dm_match"], r["phone"], r["line"],
            r["activity"], r["tier"], r["grade"], r["litigator"], r["crm_status"], r["sources"]]
           for r in rows],
          [4, 28, 24, 12, 16, 10, 9, 18, 7, 9, 11, 36])

    ws = wb.create_sheet("Local Buys")
    lb = []
    for d in dossier:
        for o in (top.get(d["rank"], {}).get("local") or []):
            lb.append([d["rank"], d["buyer"], o["address"], o["dist"], o["date"], o["price"],
                       o["exit"], o["txn"], o["beds"], o["sqft"], "Cash" if o["cash"] else ""])
    sheet(ws, "What each buyer bought near %s (deed records)" % where,
          ["#", "Buyer", "Property", "Miles", "Bought", "Paid", "Resold for", "Type", "Beds", "Sqft", "Cash"],
          lb, [4, 28, 40, 7, 11, 11, 11, 11, 6, 7, 6])
    for row in ws.iter_rows(min_row=4):
        for i in (5, 6):
            row[i].number_format = '"$"#,##0'

    ws = wb.create_sheet("Comps")
    comps = ranked.get("comps", {})
    cr = [[c["address"], c["dist"], c["date"], c["price"], c["ppsf"], c["beds"], c["baths"], c["sqft"],
           "Investor/cash" if (c["cash"] or c["entity_buyer"]) else "Retail", c["seller"], c["buyer"]]
          for c in comps.get("rows", []) if c.get("in_size")]
    sheet(ws, "Sold within %g mi, last %d months, %s-%s sqft" % (
              (subj.get("comp_mi") or getattr(a, "comp_mi", 1.0)), (subj.get("comp_months") or getattr(a, "comp_months", 18)),
              *tuple(comps.get("size_window", ["", ""]))),
          ["Address", "Miles", "Sold", "Price", "$/sf", "Beds", "Baths", "Sqft", "Kind", "Seller", "Buyer"],
          cr, [36, 7, 11, 11, 7, 6, 6, 7, 13, 28, 28])
    for row in ws.iter_rows(min_row=4):
        row[3].number_format = '"$"#,##0'

    ws = wb.create_sheet("Sources")
    src = []
    for d in dossier:
        for dm in d["decision_makers"]:
            for ev in dm.get("evidence") or [{"url": "", "quote": ""}]:
                src.append([d["rank"], d["buyer"], dm["name"], dm["confidence"], dm["source"],
                            ev.get("url", ""), ev.get("quote", "")])
        if d["research_notes"]:
            src.append([d["rank"], d["buyer"], "", "", "notes", "", d["research_notes"]])
    sheet(ws, "Where every decision maker came from",
          ["#", "Buyer", "Decision maker", "Confidence", "Source", "URL", "Quote or note"],
          src, [4, 26, 22, 10, 18, 40, 70])

    xlsx = out / ("Buyers_%s.xlsx" % a.slug)
    pending = out / ("_PENDING_Buyers_%s.xlsx" % a.slug)
    wb.save(pending)
    pending.replace(xlsx)
    with (out / "dial_sheet.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["rank"])
        w.writeheader()
        w.writerows(rows)
    print("wrote %s (%d phone rows)" % (xlsx, len(rows)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--slug", required=True)
    ap.add_argument("--phase", choices=["crm", "dossier", "trace", "verify", "export"], required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    out = ROOT / "output" / "deal_buyers" / a.slug
    {"crm": phase_crm, "dossier": phase_dossier, "trace": phase_trace, "verify": phase_verify,
     "export": phase_export}[a.phase](a, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
