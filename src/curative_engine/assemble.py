"""One row per house from everything read: the user's records and their custom fields, SiftMap's detail, the
county roll and the city's own bill, and any research already on file. Writes work/joined.json.

What each row says about the death, the title, the money and the family is the input to score.py. A house is
gated out here only for a fact no score can outweigh: a company owner with no death on file, a sale after the
death, a live listing, Do Not Market, a status of sold, or no sign anywhere that the owner died.
"""
from __future__ import annotations

import glob
import os
import re
from collections import Counter
from datetime import date, timedelta

from . import common as C
from .counties import county_for

LIEN_FLAGS = (("is_hoa_lien", "HOA lien"), ("is_other_lien", "other lien"), ("is_judgment_liens", "judgment lien"),
              ("is_federal_lien", "federal tax lien"), ("is_state_tax", "state tax lien"))
DEATH_FLAGS = (("is_pre_probate", "SiftMap pre-probate flag", 2), ("is_heirs_property", "SiftMap heirs flag", 2),
               ("is_probate_properties", "SiftMap probate flag", 2), ("is_estate_sales", "SiftMap estate flag", 2),
               ("is_obituary_property", "SiftMap obituary flag", 1), ("is_inheritance_property", "SiftMap inheritance flag", 2))
# The county appraisal is the assessment divided by the state's residential assessment ratio. SiftMap's "market
# value" is the value estimate itself, so it cannot tell a vacant lot from a house; the assessment can.
ASSESSMENT_RATIO = {"TN": 0.25}
LOAN_RATE_PCT = 5.0      # the rate an open loan's balance is estimated at when only the recorded original is known
DONE_STATUSES = {"sold", "closed", "under_contract", "under contract"}


def _flag(sm: dict, key: str) -> bool:
    v = ((sm or {}).get("distressor_data") or {}).get(key)
    return bool(v.get("is_active")) if isinstance(v, dict) else bool(v)


def _preset(sm: dict, key: str) -> bool:
    return bool(((sm or {}).get("presets") or {}).get(key))


def appraisal(sm: dict, state: str) -> float | None:
    ratio = ASSESSMENT_RATIO.get(state)
    assessed = C.money((sm or {}).get("total_assessed_value"))
    return round(assessed / ratio) if (ratio and assessed) else None


def open_loans(sm: dict, last_sale: date | None, today: date) -> tuple[float, list[dict]]:
    """(estimated balance, the loans) of the loans recorded since the last sale that are not yet due. A recorded
    loan carries only its original amount, so each balance is estimated on a level payment at LOAN_RATE_PCT over
    its own term (to its due date, else 30 years); a credit line counts half its amount (the draw is unknown)."""
    from mortgage_model import balance_after
    total, kept = 0.0, []
    for m in (sm or {}).get("mortgage_history") or []:
        if not isinstance(m, dict):
            continue
        rec_day = C.parse_day(m.get("record_date"))
        due = C.parse_day(m.get("due_date"))
        amount = C.money(m.get("amount")) or 0
        if not rec_day or amount <= 0 or (last_sale and rec_day < last_sale - timedelta(days=30)) or (due and due <= today):
            continue
        kind = str(m.get("loan_type") or "").lower()
        if "credit" in kind or "heloc" in kind:
            bal = amount * 0.5
        else:
            term = max(12, (due.year - rec_day.year) * 12 + due.month - rec_day.month) if due else 360
            elapsed = max(0, min(term, (today.year - rec_day.year) * 12 + today.month - rec_day.month))
            bal = balance_after(amount, LOAN_RATE_PCT, term, elapsed)
        total += max(0.0, bal)
        kept.append({"recorded": C.iso(rec_day), "amount": amount, "type": m.get("loan_type"), "balance_est": round(bal)})
    return round(total, 2), kept


def deed_people(sm: dict, rec: dict) -> list[str]:
    """The owners of record: SiftMap's deed names (a county string can join two people with '&'), else the
    record's owner and second owners."""
    out = []
    oi = (sm or {}).get("owner_info") or {}
    names = [oi.get("owner_name")] + list(oi.get("secondary_owner_names") or [])
    for v in names:
        for part in re.split(r"\s*&\s*|\s*;\s*", str(v or "")):
            part = part.strip(" ,")
            if len([t for t in C.toks(part) if t not in C.NOISE]) >= 2 and part not in out:
                out.append(part)
    if not out:
        o = (rec or {}).get("owner") or {}
        first = " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x).strip()
        if first:
            out.append(first)
        for s in (rec or {}).get("secondary_owners") or []:
            if isinstance(s, dict):
                nm = s.get("full_name") or " ".join(x for x in (s.get("first_name"), s.get("last_name")) if x)
                if nm and nm not in out:
                    out.append(nm)
    return out


def tax_view(t: dict | None) -> dict:
    """Delinquent county tax from the live bills, the roll owner, and who paid the last bill."""
    out = {"status": (t or {}).get("status") or "not read", "owner": "", "mailing": "", "amount": 0.0, "years": [],
           "turned_over": False, "last_payor": "", "last_pay_date": None, "last_paid_year": None, "current_due": 0.0,
           "city_codes": [], "account": "", "assessor_url": "", "accounts": 0, "partial": ""}
    if not t or t.get("status") not in ("ok", "ambiguous", "no bills"):
        return out
    accts = t.get("accounts") or []
    out["accounts"] = len(accts)
    pick = next((a for a in accts if a.get("account") == t.get("chosen")), None)
    if pick is None and accts:
        reals = [a for a in accts if any((b.get("type") or "").lower() == "real" for b in a.get("bills") or [])]
        pick = (reals or accts)[0]
    if not pick:
        return out
    out["owner"] = pick.get("owner") or ""
    out["account"] = pick.get("account") or ""
    out["assessor_url"] = pick.get("assessor_url") or ""
    bills = [b for b in pick.get("bills") or [] if (b.get("type") or "real").lower() == "real"] or (pick.get("bills") or [])
    codes = set()
    for b in bills:
        if b.get("mailing"):
            out["mailing"] = b["mailing"]
        cn = str(b.get("city_no") or "").strip()
        if cn and cn not in ("0", "None"):
            codes.add(cn)
        if b.get("delinquent") and not b.get("paid") and (b.get("due") or 0) > 0:
            out["amount"] += b["due"]
            out["years"].append(b["year"])
            if b.get("turned_over"):
                out["turned_over"] = True
            if b.get("payor"):
                pp = re.sub(r"\s*\(.*$", "", str(b["payor"])).strip()
                out["partial"] = "a payment plan or bank draft" if C.CHANNEL_RX.search(pp) else pp
        elif not b.get("delinquent") and not b.get("paid"):
            out["current_due"] += b.get("due") or 0
    paid = [b for b in bills if b.get("paid") and b.get("pay_date")]
    if paid:
        lastb = max(paid, key=lambda b: (b.get("year") or "", b.get("pay_date") or ""))
        out["last_payor"] = re.sub(r"\s*\(.*$", "", str(lastb.get("payor") or "")).strip()
        out["last_pay_date"] = lastb.get("pay_date")
        out["last_paid_year"] = lastb.get("year")
    out["amount"] = round(out["amount"], 2)
    out["years"] = sorted(set(out["years"]))
    out["city_codes"] = sorted(codes)
    return out


def research_files(roots=None) -> dict:
    """slug -> the newest deep prospect research file (owner status, decedents) on this machine, if any."""
    roots = roots if roots is not None else [C.ROOT / "output" / "deep_prospect", C.ROOT / "output" / "agents" / "runs"]
    out = {}
    for root in roots:
        for p in glob.glob(os.path.join(str(root), "*", "research", "*.json")):
            if p.endswith(("_urls.json", "_open.json")):
                continue
            js = C.read_json(p)
            if not isinstance(js, dict) or "owner_status" not in js:
                continue
            s = os.path.basename(p)[:-5]
            prev = out.get(s)
            if prev is None or os.path.getmtime(p) > prev[0]:
                out[s] = (os.path.getmtime(p), js)
    return {k: v[1] for k, v in out.items()}


def _custom(cf: dict, *labels):
    low = {str(k).strip().lower(): v for k, v in (cf or {}).items()}
    for lab in labels:
        v = low.get(lab.lower())
        if v not in (None, ""):
            return v
    return None


def _newest_obit(sm: dict) -> date | None:
    best = None
    for o in (sm or {}).get("obituary_history") or []:
        if isinstance(o, dict):
            d = C.from_epoch(o.get("date") or o.get("obituary_date") or o.get("published") or o.get("death_date"))
            if d and (best is None or d > best):
                best = d
    return best


def _sale_after_death(sm: dict, rec: dict, death: date | None, family: list[str]) -> tuple[date | None, bool]:
    """(the last sale, whether it was an arm's length sale after the death). A transfer to someone with a
    family surname, or for under $10,000, is a family transfer, not a sale away."""
    sales = [s for s in (sm or {}).get("sale_history") or [] if isinstance(s, dict) and C.parse_day(s.get("sale_date"))]
    sales.sort(key=lambda s: C.parse_day(s.get("sale_date")), reverse=True)
    last = C.parse_day(sales[0].get("sale_date")) if sales else C.parse_day((rec or {}).get("last_sold"))
    if not (last and death and last > death):
        return last, False
    if sales:
        s = sales[0]
        price = C.money(s.get("sale_price")) or 0
        buyer = set(C.toks(s.get("buyer_name")))
        fam = {C.name_toks(n)[-1] for n in family if C.name_toks(n)}
        if price < 10000 or (fam & buyer):
            return last, False
    return last, True


def build(records: dict, details: dict, county_reads: dict, city_reads: dict, research: dict | None = None,
          scope: list | None = None) -> list[dict]:
    """The joined rows. `records` uuid -> {record, custom, via}; `details` dataflik_id -> SiftMap detail;
    `county_reads` uuid -> roll read; `city_reads` account -> city bill."""
    research = research if research is not None else {}
    today = C.today()
    keep = set(scope) if scope else None
    by_house: dict = {}
    for u, x in records.items():
        if keep is not None and u not in keep:
            continue
        rec = (x or {}).get("record") or {}
        if not rec:
            continue
        df = rec.get("dataflik_id")
        key = "df:%s" % df if df else "crm:%s" % u
        by_house.setdefault(key, []).append((u, x))
    probate_names = []
    for u, x in records.items():
        rec, cf = (x or {}).get("record") or {}, (x or {}).get("custom") or {}
        if "Probate" in (rec.get("lists") or []):
            dec = _custom(cf, "Decedent Name") or ""
            month = next((t for t in sorted(rec.get("tags") or []) if re.fullmatch(r"20\d\d-\d\d", str(t))), None)
            probate_names.append({"uuid": u, "decedent": dec, "df": rec.get("dataflik_id"), "month": month,
                                  "opened": C.iso(C.parse_day(rec.get("probate_open_date") or _custom(cf, "Probate Open Date")))})
    rows = []
    for key, members in by_house.items():
        recs = [m[1]["record"] for m in members]
        cfs: dict = {}
        for m in members:
            cfs.update({k: v for k, v in ((m[1] or {}).get("custom") or {}).items() if v not in (None, "")})
        rec = recs[0]
        df = rec.get("dataflik_id")
        sm = ((details or {}).get(str(df)) or {}) if df else {}
        if sm.get("_error"):
            sm = {}
        addr = rec.get("address") or {}
        street = addr.get("street") or ""
        city = addr.get("city") or ""
        zip5 = str(addr.get("postal_code") or "")[:5]
        prop_state = C.state_code(addr.get("state")) or "TN"
        cty = county_for(fips=sm.get("fips") or "", name=addr.get("county") or sm.get("county") or "", state=prop_state)
        lists = sorted({l for r in recs for l in (r.get("lists") or [])})
        tags = sorted({t for r in recs for t in (r.get("tags") or [])})
        statuses = sorted({str(r.get("status") or "").strip().lower() for r in recs if r.get("status")})
        roll_read = None
        for m in members:
            roll_read = roll_read or (county_reads or {}).get(m[0])
        t = tax_view(roll_read)
        cit = (city_reads or {}).get(t["account"].replace("-", "").replace(" ", "")) if t["account"] else None
        this_year = str(today.year)
        city_years = sorted({y["year"] for y in (cit or {}).get("years") or [] if (y.get("owed") or 0) > 0 and y["year"] < this_year})
        city_amt = round(sum(y["owed"] for y in (cit or {}).get("years") or [] if (y.get("owed") or 0) > 0 and y["year"] < this_year), 2)
        if cit and (cit.get("prior") or 0) > city_amt:
            city_amt = cit["prior"]
        deed = deed_people(sm, rec)
        roll, roll_etal = C.roll_people(t["owner"])

        # ---- who died
        evidence, decedents, death_day, conf = [], [], None, 0
        dec_named = str(_custom(cfs, "Decedent Name") or "").strip()
        notice = str(_custom(cfs, "Notice Type") or "").lower()
        dec_court = dec_named if ("Probate" in lists and notice == "probate") else ""
        dec_record = "" if dec_court else dec_named
        owner_dead_field = str(_custom(cfs, "Owner Deceased") or "").strip().lower()
        rs = research.get(C.slug(street))
        if dec_court:
            decedents.append(dec_court)
            evidence.append("probate notice names %s" % C.nice(dec_court))
            conf = max(conf, 3)
        if dec_record and len([w for w in C.toks(dec_record) if w not in C.NOISE]) <= 4 and "&" not in dec_record:
            decedents.append(dec_record)
            evidence.append("the record names %s as the decedent" % C.nice(dec_record))
            conf = max(conf, 3 if owner_dead_field == "yes" else 2)
        if rs:
            st = str(rs.get("owner_status") or "").lower()
            for dcd in rs.get("decedents") or []:
                if isinstance(dcd, dict) and dcd.get("name"):
                    decedents.append(dcd["name"])
                    d0 = C.parse_day(dcd.get("dod"))
                    if d0 and (death_day is None or d0 > death_day):
                        death_day = d0
            if st == "deceased":
                conf = max(conf, 3)
                evidence.append("our research: owner deceased")
            elif st == "living":
                evidence.append("our research: owner living")
        owner_flag_dead = any(((r.get("owner") or {}).get("deceased")) for r in recs)
        if owner_flag_dead:
            conf = max(conf, 3)
            evidence.append("skip trace marks the owner deceased")
        if owner_dead_field == "yes" and not dec_record:
            conf = max(conf, 2)
            evidence.append("record field Owner Deceased")
        dod_cf = C.parse_day(_custom(cfs, "Date of Death", "Death Date"))
        if dod_cf:
            death_day = death_day or dod_cf
        if C.is_estate(t["owner"]) or any(C.is_estate(x) for x in deed):
            conf = max(conf, 3)
            evidence.append("owner name reads estate or heirs")
        for f, lab, c in DEATH_FLAGS:
            if _flag(sm, f):
                conf = max(conf, c)
                evidence.append(lab)
        obit_day = C.from_epoch(rec.get("last_obituary_date")) or _newest_obit(sm)
        if "Obituary" in lists and conf < 1:
            conf = 1
            evidence.append("on the Obituary list")
        if "Verify Deceased" in tags:
            evidence.append("tagged Verify Deceased")
            conf = max(conf, 2 if ("Obituary" in lists or obit_day) else 1)
        if obit_day and not death_day:
            death_day = obit_day
        if rs and str(rs.get("owner_status") or "").lower() == "living" and not dec_court:
            conf = min(conf, 1)

        # ---- is the decedent still the title holder?
        title_holder_dead = None
        if decedents:
            on_roll = any(C.person_on(d, t["owner"]) for d in decedents) if t["owner"] else None
            on_deed = any(C.person_on(d, " ".join(deed)) for d in decedents) if deed else None
            title_holder_dead = bool(on_roll) if on_roll is not None else on_deed
        estate_named = C.is_estate(t["owner"])
        life_estate = bool(C.LIFE_EST_RX.search(t["owner"] or "")) or any(C.LIFE_EST_RX.search(x) for x in deed)

        # ---- probate on file in the account
        names = list(decedents) + deed[:2] + roll[:2]
        hits = [p for p in probate_names
                if (df and p["df"] and str(p["df"]) == str(df))
                or (p["decedent"] and any(C.person_on(p["decedent"], n) for n in names if n))]
        probate_open = bool(hits) or "Probate" in lists or _flag(sm, "is_probate_properties")
        probate_month = None
        for p in hits:
            m_ = (p.get("opened") or "")[:7] or p.get("month")
            if m_ and (probate_month is None or m_ < probate_month):
                probate_month = m_
        if probate_open and not death_day and probate_month:
            y, mo = probate_month.split("-")[:2]
            death_day = date(int(y), int(mo), 1)      # a death precedes its probate notice: a floor, not the date

        # ---- sale after the death, a listing
        last_sale, sold_after = _sale_after_death(sm, rec, death_day, decedents + deed)
        mls = str(rec.get("mls") or "").strip()
        listed = mls.lower() in ("listed", "active", "pending")

        # ---- money
        avm = C.money(rec.get("estimate_value"))
        appraised = appraisal(sm, prop_state)
        eq_pct = C.money(rec.get("equity_percent"))
        ltv = C.money(rec.get("loan_to_value"))
        loan_bal, loans = open_loans(sm, last_sale, today)
        mort, mort_src = None, ""
        if avm and eq_pct is not None and eq_pct < 99.5:
            mort, mort_src = max(0.0, avm * (1 - eq_pct / 100.0)), "the record's equity estimate"
        elif avm and ltv:
            mort, mort_src = max(0.0, avm * ltv / 100.0), "the record's loan to value"
        elif _preset(sm, "free_clear"):
            mort, mort_src = 0.0, "SiftMap: free and clear"
        elif loans:
            mort, mort_src = loan_bal, "estimated from %d recorded loan(s)" % len(loans)
        elif sm or eq_pct is not None:
            mort, mort_src = 0.0, "no open loan of record"
        free_clear = mort == 0.0
        crm_lien = C.money(_custom(cfs, "Outstanding Lien Amount"))
        lien_flags = [lab for f, lab in LIEN_FLAGS if _flag(sm, f)]
        lien_amt = crm_lien or 0.0
        debt = (mort or 0) + t["amount"] + (city_amt or 0) + lien_amt
        equity = (avm - debt) if (avm and mort is not None) else None
        carry = int(equity // 50000) if equity and equity > 0 else 0

        # ---- foreclosure
        fc_list = [f for f in (sm.get("foreclosure") or []) if isinstance(f, dict)]
        sale_day = (C.parse_day(_custom(cfs, "Foreclosure Date")) or C.from_epoch(rec.get("foreclosure_date"))
                    or C.from_epoch(rec.get("auction_date"))
                    or next((C.from_epoch(f.get("auction_date")) for f in fc_list if C.from_epoch(f.get("auction_date"))), None))
        fc_ahead = bool(sale_day and sale_day >= today)
        in_fc = "Foreclosure" in lists or _flag(sm, "is_notice_of_default") or _flag(sm, "is_notice_of_foreclosure")

        # ---- occupancy and contact
        o = rec.get("owner") or {}
        omail = o.get("address") or {}
        mail_state = C.state_code(omail.get("state")) or ""
        roll_mail = t["mailing"] or ""
        num = (street.split() or [""])[0]
        st_t = C.toks(street)
        roll_mail_here = bool(roll_mail and num and roll_mail.upper().startswith(num + " ") and st_t and st_t[0] in roll_mail.upper())
        mail_here = bool(omail.get("street")) and C.street_key(omail.get("street")) == C.street_key(street)
        absentee = _preset(sm, "absentee_owners") or bool(roll_mail and not roll_mail_here) or (bool(omail.get("street")) and not mail_here)
        oos = bool(mail_state and mail_state != prop_state)
        vacant = bool(addr.get("vacant")) or _flag(sm, "is_vacant_property") or _preset(sm, "vacant")
        payor, payor_kind = t["last_payor"], ""
        if payor and C.CHANNEL_RX.search(payor):
            payor = ""
        if payor:
            if C.LENDER_RX.search(payor) or re.search(r"\bUSDA\b|RURAL DEV", payor, re.I):
                payor_kind = "lender or service"
            else:
                holders = [*deed, *roll]
                if any(C.person_on(payor, x) or C.person_on(x, payor) for x in holders):
                    payor_kind = "owner"
                elif any(set(C.toks(payor)) & set(C.toks(x)[-1:] + C.toks(x)[:1]) for x in holders):
                    payor_kind = "family (same surname)"
                else:
                    payor_kind = "someone else"

        # ---- gates
        gate = ""
        named = [deed[0] if deed else "", t["owner"] or "", o.get("company") or ""]
        company = any(C.is_entity(x) and not C.TRUST_RX.search(x) for x in named if x)
        if company and not estate_named and not decedents:
            gate = "company owner"
        elif set(statuses) & DONE_STATUSES:
            gate = "status %s" % sorted(set(statuses) & DONE_STATUSES)[0]
        elif sold_after:
            gate = "sold after the death (%s)" % last_sale
        elif listed:
            gate = "listed on the MLS (%s)" % mls
        elif "Do Not Market" in tags or "Estate Sold" in tags:
            gate = "Do Not Market" if "Do Not Market" in tags else "estate sold"
        elif conf == 0 and not in_fc:
            gate = "no sign the owner died"
        dms = [(_custom(cfs, a), _custom(cfs, b)) for a, b in (("Decision Maker", "DM Relationship"),
                                                                 ("DM 2 Name", "DM 2 Relationship"),
                                                                 ("DM 3 Name", "DM 3 Relationship"))]
        rows.append({
            "key": key, "uuids": [m[0] for m in members], "dataflik_id": df, "street": street, "city": city, "zip": zip5,
            "state": prop_state, "county": cty.name, "fips": cty.fips, "parcel": rec.get("parcel_id") or rec.get("apn"),
            "lists": lists, "tags": tags, "status": ", ".join(statuses), "via": sorted({v for m in members for v in (m[1].get("via") or [])}),
            "deed": deed, "deed_count": len(roll) if roll else len(deed), "roll_owner": t["owner"], "roll_people": roll,
            "roll_etal": roll_etal, "roll_status": t["status"], "dec_record": dec_record, "owner_dead_field": owner_dead_field,
            "life_estate": life_estate, "dms": [list(x) for x in dms if x[0]],
            "roll_mail": roll_mail, "roll_mail_here": roll_mail_here, "estate_named": estate_named,
            "trust": any(C.TRUST_RX.search(x) for x in deed) or bool(C.TRUST_RX.search(t["owner"] or "")),
            "decedents": decedents, "decedent_court": dec_court, "death_conf": conf, "evidence": evidence,
            "death_day": C.iso(death_day), "obit_day": C.iso(obit_day), "months_since_death": C.months_between(death_day),
            "owner_age": o.get("age"), "title_holder_dead": title_holder_dead,
            "research_status": (rs or {}).get("owner_status"), "probate_open": probate_open, "probate_month": probate_month,
            "probate_hits": [p["uuid"] for p in hits][:3], "last_sale": C.iso(last_sale), "sold_after": sold_after,
            "mls": mls, "listed": listed, "avm": avm, "appraised": round(appraised) if appraised else None,
            "mortgage": round(mort) if mort is not None else None, "mortgage_source": mort_src, "loans": loans,
            "free_clear": free_clear,
            "tax_status": t["status"], "tax_amount": t["amount"], "tax_years": t["years"], "turned_over": t["turned_over"],
            "tax_current_due": round(t["current_due"], 2), "last_payor": payor, "partial_payor": t["partial"],
            "payor_kind": payor_kind, "last_pay_date": t["last_pay_date"], "last_paid_year": t["last_paid_year"],
            "city_codes": t["city_codes"], "in_city": bool(cit) or bool(set(t["city_codes"]) & set(cty.city_codes)),
            "assessor_url": t["assessor_url"], "city_read": bool(cit), "city_amount": city_amt if cit else None,
            "city_years": city_years, "city_suit": bool((cit or {}).get("suit")), "account": t["account"],
            "lien_flags": lien_flags, "crm_lien": crm_lien, "lien_amount": round(lien_amt, 2),
            "debt": round(debt), "equity": round(equity) if equity is not None else None, "carry": carry,
            "sale_day": C.iso(sale_day), "fc_ahead": fc_ahead, "in_fc": in_fc, "absentee": bool(absentee), "oos": oos,
            "mail_state": mail_state, "vacant": bool(vacant), "year_built": rec.get("year") or sm.get("years_built"),
            "sqft": rec.get("sqft") or sm.get("living_square_feet"), "beds": rec.get("bedrooms"), "baths": rec.get("bathrooms"),
            "investor_score": rec.get("investor_score"),
            "calls": max([r.get("predictivecall_attempts") or 0 for r in recs] or [0]),
            "texts": max([r.get("sms_attempts") or 0 for r in recs] or [0]),
            "has_phones": any(((r.get("owner") or {}).get("phone_count") or 0) > 0 for r in recs),
            "gate": gate, "owner_name": " ".join(x for x in (o.get("first_name"), o.get("last_name")) if x).strip(),
            "pr": str(_custom(cfs, "Personal Representative") or rec.get("personal_representative") or ""),
            "decision_maker": str(_custom(cfs, "Decision Maker") or ""), "obit_url": str(_custom(cfs, "Obituary URL") or ""),
            "sources": cty.sources(),
        })
    return rows


def run(ws, out=print) -> list[dict]:
    scope = (ws.read("scope.json", {}) or {}).get("uuids")
    rows = build(ws.read("records.json", {}) or {}, ws.read("details.json", {}) or {}, ws.read("county.json", {}) or {},
                 ws.read("city.json", {}) or {}, research_files(), scope)
    ws.write("joined.json", rows, indent=None)
    out("houses %d | gates %s" % (len(rows), dict(Counter(r["gate"] or "kept" for r in rows).most_common(8))))
    out("county roll %s | death signal %s" % (dict(Counter(r["roll_status"] for r in rows).most_common(6)),
                                              dict(Counter(r["death_conf"] for r in rows))))
    return rows
