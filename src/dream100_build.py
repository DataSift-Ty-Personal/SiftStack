"""Dream 100 scoring + workbook. Imported by dream100.py (--phase build).

Opportunity score (0-100), every component visible on the sheet:
  Deal size 35   discount room = min(equity $, 30% of value), ramp $15K..$120K
  Condition 20   D4D visual distress score, ramp 69..100 (the cohort floor is 69)
  Investor  20   DataFlik investor_score ramp 20..90, minus up to 5 when
                 realtor_score >= 70 (an agent is likely to win that one)
  Evidence  15   distress evidence: attached lists + record fields, capped
  Reach     10   best dial tier on the owner's phones; DNC/opt-out = mail only

Deal size is the seller's discount room, not a profit forecast: a seller cannot
sell below the payoff, and the 70% rule gap is the most a wholesale buyer pays for.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

import d4d_call_list as d4d

OUT = Path("output/dream100")
FULL_PATH = OUT / "full_records.json"
NAVY, BLUE, GREEN, GOLD, RED = "0A1130", "316AFF", "1B9E5A", "B7791F", "CC0000"
INK2, LINE, BG = "4A5068", "D9DEE8", "F5F7FB"
BUY_BOX_MAX = 700_000
W_DEAL, W_COND, W_INV, W_EVID, W_REACH = 35, 20, 20, 15, 10
DEAL_LO, DEAL_HI = 15_000, 120_000
DEAD_STATUSES = {"sold", "closed", "not_interested", "buyer", "dead", "lost", "not interested"}
WORKED_STATUSES = {"prospecting", "deep prospecting", "new_lead", "follow_up",
                   "no contact new lead", "offer_made", "appointment_set", "under_contract"}
DEAD_TAGS = {"recently sold", "do not market"}
TIER_PTS = {"Dial First": 10, "Dial Second": 7, "Dial Third": 4, "Dial Fourth": 2, "Drop": 0, "": 0}

# distress evidence points per attached list (lists the account itself attached)
LIST_PTS = {
    "Foreclosure": 5, "Pre-Foreclosures - Notice of Default": 5, "Pre-Foreclosures - Lis Pendens": 5,
    "Trustee Deed": 5, "Tax Delinquent": 4, "Vacant": 4, "Probate": 4, "Pre-Probate/Deceased": 4,
    "Obituary": 4, "Estate Sales": 4, "Condemned": 4, "Code Violation": 4, "Liens": 3,
    "Bankruptcy": 3, "Divorce": 3, "Judgments": 3, "Tired Landlord": 2, "Eviction": 2,
    "Low Credit Score": 2, "Out-of-State": 2, "Low Income": 1, "Absentee Owners": 1,
    "Senior Homeowners": 1,
}
NON_EVIDENCE = {"D4D Top 3000", "Free & Clear", "Owner Occupied", "High Equity", "Low Equity",
                "Negative Equity", "Dispo - Flip Buyers", "Dispo - VIP Buyers", "Dream 100"}
EVID_CAP = 14.0  # raw points at which evidence = full 15


# ------------------------------------------------------------- helpers --

def num(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def ramp(v, lo, hi):
    if v is None:
        return 0.0
    if v <= lo:
        return 0.0
    if v >= hi:
        return 1.0
    return (v - lo) / (hi - lo)


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def no_dash(s):
    return str(s).replace(chr(0x2014), "-").replace(chr(0x2013), "-") if s is not None else s


def fmt_phone(n):
    n = re.sub(r"\D", "", str(n or ""))
    if len(n) == 11 and n[0] == "1":
        n = n[1:]
    return "(%s) %s-%s" % (n[:3], n[3:6], n[6:]) if len(n) == 10 else n


def is_entity(ow):
    return (ow.get("type") or "").lower() != "person" or bool(ow.get("company"))


def owner_name(ow):
    if ow.get("company"):
        return ow["company"]
    return (" ".join(x for x in [ow.get("first_name"), ow.get("last_name")] if x)).strip()


# ------------------------------------------------------------ flatten --

def flatten(rec: dict, co: dict, scores: dict) -> dict:
    ow = rec.get("owner") or {}
    oa = ow.get("address") or {}
    a = rec.get("address") or {}
    lists = [(l.get("title") if isinstance(l, dict) else l) for l in rec.get("lists") or []]
    tags = d4d.tag_names(rec)
    value = num(rec.get("estimate_value"))
    eq_pct = num(rec.get("equity_percent"))
    eq_dollars = value * eq_pct / 100.0 if value is not None and eq_pct is not None else None
    last_price = num(rec.get("last_sale_price"))
    last_sold = parse_date(rec.get("last_sold")) or parse_date(rec.get("owned_since"))
    years_held = round((date.today() - last_sold).days / 365.25, 1) if last_sold else None
    rent = num(rec.get("rental_value"))
    tax_owed = num(rec.get("tax_delinquent_value"))
    phones = ow.get("phones") or []
    tiers = []
    for p in phones:
        st = (p.get("status") or "").upper()
        if st in d4d.BAD_STATUSES:
            continue
        t = d4d.phone_tier(p)
        sc = scores.get(re.sub(r"\D", "", str(p.get("number") or "")))
        if not t and sc:
            t = sc.get("tier") or ""
        tiers.append((p, t))
    order = {"Dial First": 0, "Dial Second": 1, "Dial Third": 2, "Dial Fourth": 3, "": 4, "Drop": 5}
    tiers.sort(key=lambda x: (order.get(x[1], 4), 0 if (x[0].get("type") or "") == "MOBILE" else 1))
    n_first = sum(1 for _, t in tiers if t == "Dial First")
    n_second = sum(1 for _, t in tiers if t == "Dial Second")
    best_tier = tiers[0][1] if tiers else ""
    dnc = bool(ow.get("dnc") or ow.get("opt_out"))
    status = (rec.get("status") or "").strip()
    st_l = status.lower()
    attempts = sum(int(rec.get(k) or 0) for k in
                   ("predictivecall_attempts", "sms_attempts", "rvm_attempts", "directmail_attempts"))
    mail_abs = (oa.get("street") or "").strip().lower() != (a.get("street") or "").strip().lower()
    out_of_state = (oa.get("state") or "").upper() not in ("", "TN")
    return {
        "uuid": rec.get("uuid"), "dataflik_id": rec.get("dataflik_id"),
        "d4d_rank": co.get("rank"), "d4d_score": co.get("score"),
        "street": a.get("street") or "", "city": a.get("city") or "", "zip": str(a.get("postal_code") or "")[:5],
        "county": a.get("county") or co.get("county") or "", "neighborhood": rec.get("neighborhood") or co.get("neighborhood") or "",
        "lat": a.get("latitude"), "lon": a.get("longitude"),
        "owner": owner_name(ow), "owner_first": ow.get("first_name") or "", "owner_last": ow.get("last_name") or "",
        "owner_company": ow.get("company") or "", "entity": is_entity(ow), "owner_uuid": ow.get("uuid"),
        "secondary_owners": ", ".join((s.get("full_name") or "") for s in rec.get("secondary_owners") or []),
        "age": ow.get("age"), "deceased": bool(ow.get("deceased")),
        "mail_street": oa.get("street") or "", "mail_city": oa.get("city") or "", "mail_state": oa.get("state") or "",
        "mail_zip": str(oa.get("postal_code") or "")[:5], "absentee": mail_abs, "out_of_state": out_of_state,
        "value": value, "equity_pct": eq_pct, "equity_dollars": eq_dollars,
        "last_price": last_price, "last_sold": last_sold, "years_held": years_held,
        "gain": (value - last_price) if value is not None and last_price else None,
        "rent": rent, "yield": (rent * 12 / value * 100) if rent and value else None,
        "ltv": num(rec.get("loan_to_value")), "open_mortgages": rec.get("open_mortgages"),
        "mortgage_type": rec.get("mortgage_type") or "",
        "tax_owed": tax_owed, "tax_year": rec.get("tax_delinquent_year") or "",
        "years_behind": rec.get("year_behind_on_taxes"), "total_taxes": num(rec.get("total_taxes")),
        "lien_type": rec.get("lien_type") or "", "lien_date": rec.get("lien_recording_date") or "",
        "foreclosure_date": rec.get("foreclosure_date") or "", "auction_date": rec.get("auction_date") or rec.get("tax_auction_date") or "",
        "probate_open": rec.get("probate_open_date") or "", "pr": rec.get("personal_representative") or "",
        "bankruptcy_date": rec.get("bankruptcy_recording_date") or "", "divorce_date": rec.get("divorce_file_date") or "",
        "obit_date": rec.get("last_obituary_date") or "",
        "vacant": bool(a.get("vacant")) or ("Vacant" in lists),
        "mls": rec.get("mls") or "", "structure": rec.get("structure_type") or "",
        "beds": rec.get("bedrooms"), "baths": rec.get("bathrooms"), "sqft": num(rec.get("sqft")),
        "year": rec.get("year"), "lot": rec.get("lot_size"),
        "investor_score": num(rec.get("investor_score")), "realtor_score": num(rec.get("realtor_score")),
        "on_market_score": num(rec.get("on_market_score")),
        "lists": lists, "tags": tags, "status": status, "status_l": st_l,
        "assigned": rec.get("assigned_to") or "", "attempts": attempts, "hotness": rec.get("hotness") or 0,
        "dnc": dnc, "do_not_mail": bool(ow.get("do_not_mail_ever") or rec.get("do_not_mail_ever")),
        "skiptraced": bool(ow.get("skiptraced")), "n_phones": len(phones), "n_emails": len(ow.get("emails") or []),
        "phones": [(fmt_phone(p.get("number")), (p.get("type") or "").title(), t) for p, t in tiers[:5]],
        "best_tier": best_tier, "n_first": n_first, "n_second": n_second,
        "known_distress": co.get("known_distress") or "",
        "crm_url": d4d.CRM_URL % rec.get("uuid"),
    }


# ------------------------------------------------------------- gates --

def gate_reason(r: dict):
    if r["status_l"] in DEAD_STATUSES:
        return "Lead status %s" % (r["status"] or "dead")
    if any(t.lower() in DEAD_TAGS for t in r["tags"]):
        return "Tagged " + ", ".join(t for t in r["tags"] if t.lower() in DEAD_TAGS)
    if any(l.startswith("Dispo - ") for l in r["lists"]):
        return "On a Dispo buyer list (the owner is a buyer, not a seller)"
    if r["value"] is None or r["value"] <= 0:
        return "No value on record"
    if r["value"] > BUY_BOX_MAX:
        return "Over the $700K buy box"
    s = r["structure"].lower()
    if s and any(k in s for k in ("multi", "duplex", "triplex", "quad", "condo", "town", "mobile",
                                  "manufactured", "commercial", "vacant land", "lot", "apartment")):
        return "Not single family (%s)" % r["structure"]
    if r["equity_dollars"] is not None and r["equity_dollars"] <= 0:
        return "Upside down (negative equity)"
    if r["mls"] and r["mls"].lower() not in ("off market", "", "none"):
        return "Listed on the MLS (%s)" % r["mls"]
    return None


# ------------------------------------------------------------- score --

def score(r: dict) -> dict:
    v = r["value"] or 0.0
    room = 0.30 * v
    if r["equity_dollars"] is not None:
        room = min(room, r["equity_dollars"])
    r["deal_size"] = round(room, -2)
    pts_deal = W_DEAL * ramp(room, DEAL_LO, DEAL_HI)
    pts_cond = W_COND * ramp(num(r["d4d_score"]), 69, 100)
    inv = W_INV * ramp(r["investor_score"], 20, 90)
    if r["realtor_score"] is not None and r["realtor_score"] >= 70:
        inv -= 5 * ramp(r["realtor_score"], 70, 100)
    pts_inv = max(inv, 0.0)

    ev = 0.0
    why_ev = []
    for l in r["lists"]:
        p = LIST_PTS.get(l, 0)
        if p:
            ev += p
            why_ev.append(l)
    if r["vacant"] and "Vacant" not in r["lists"]:
        ev += 4; why_ev.append("vacant flag")
    if r["tax_owed"]:
        ev += 3; why_ev.append("tax owed $%s" % format(int(r["tax_owed"]), ","))
    if r["deceased"]:
        ev += 4; why_ev.append("owner deceased")
    if r["foreclosure_date"] or r["auction_date"]:
        ev += 5; why_ev.append("auction/foreclosure date on record")
    if r["probate_open"]:
        ev += 4; why_ev.append("probate open")
    if r["bankruptcy_date"]:
        ev += 3; why_ev.append("bankruptcy")
    if r["divorce_date"]:
        ev += 3; why_ev.append("divorce")
    if r["age"] and r["age"] >= 75:
        ev += 1; why_ev.append("owner %d" % int(r["age"]))
    if r["years_held"] and r["years_held"] >= 20:
        ev += 1; why_ev.append("held %d yrs" % int(r["years_held"]))
    if r["out_of_state"] and "Out-of-State" not in r["lists"]:
        ev += 2; why_ev.append("out of state owner")
    r["evidence_raw"] = ev
    pts_ev = W_EVID * ramp(ev, 0, EVID_CAP)

    if r["dnc"]:
        pts_reach = 0.0
        reach = "DNC, mail only"
    else:
        pts_reach = float(TIER_PTS.get(r["best_tier"], 0))
        reach = r["best_tier"] or ("phones untiered" if r["n_phones"] else "no phone")
    r["reach"] = reach
    r["pts"] = {"deal": round(pts_deal, 1), "cond": round(pts_cond, 1), "inv": round(pts_inv, 1),
                "evid": round(pts_ev, 1), "reach": round(pts_reach, 1)}
    r["opp"] = round(pts_deal + pts_cond + pts_inv + pts_ev + pts_reach, 1)

    why = ["$%s discount room" % format(int(r["deal_size"]), ",")]
    if r["equity_dollars"] is not None and r["equity_dollars"] < 0.30 * v:
        why[-1] += " (capped by equity)"
    why.append("D4D %s" % r["d4d_score"])
    if r["investor_score"] is not None:
        why.append("investor %d" % r["investor_score"])
    if r["realtor_score"] is not None and r["realtor_score"] >= 70:
        why.append("realtor %d (agent risk)" % r["realtor_score"])
    if why_ev:
        why.append("evidence: " + ", ".join(why_ev[:6]))
    why.append(reach)
    r["why"] = "; ".join(why)

    verify = []
    verify.append("Value is an AVM, not an appraisal; comp it before an offer")
    if r["d4d_score"] and r["d4d_score"] >= 90:
        verify.append("visual distress from the last street pano; confirm condition with a drive-by")
    if r["status_l"] in WORKED_STATUSES:
        verify.append("already in the pipeline (%s); coordinate with the assignee" % r["status"])
    if r["attempts"]:
        verify.append("%d prior touches on record" % r["attempts"])
    if r["entity"]:
        verify.append("entity owner; find the signing member before the call")
    if r["deceased"] or "Obituary" in r["lists"] or "Probate" in r["lists"] or "Pre-Probate/Deceased" in r["lists"]:
        verify.append("possible estate: match the decedent to the owner of record before treating it as an heir case")
    if r["dnc"]:
        verify.append("DNC on the owner; direct mail or door knock only")
    if r["do_not_mail"]:
        verify.append("do-not-mail set on the record")
    if r["equity_dollars"] is not None and r["equity_dollars"] < 0.30 * v:
        verify.append("payoff limits the discount; verify the real balance at the Register of Deeds")
    r["verify"] = "; ".join(verify)
    return r


# ---------------------------------------------------------- workbook --

def _hdr(ws, row, headers):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF", size=10)
        c.fill = PatternFill("solid", fgColor=NAVY)
        c.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30


def _title(ws, text, sub=""):
    ws.cell(row=1, column=1, value=text).font = Font(bold=True, size=15, color=NAVY)
    if sub:
        ws.cell(row=2, column=1, value=sub).font = Font(size=10, color=INK2)


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


MAIN_COLS = [
    ("Rank", 6), ("Opp score", 8), ("Deal size $", 12), ("Address", 26), ("City", 12), ("Zip", 7),
    ("Neighborhood", 16), ("County", 8), ("Owner", 24), ("Owner type", 9), ("Age", 5),
    ("D4D score", 7), ("Investor score", 8), ("Realtor score", 8), ("On-market score", 8),
    ("Value", 11), ("Equity %", 8), ("Equity $", 11), ("Last sale $", 10), ("Last sold", 10),
    ("Yrs held", 7), ("Rent est", 8), ("Gross yield %", 8), ("LTV %", 7), ("Mortgage", 12),
    ("Tax owed", 9), ("Vacant", 6), ("Lists attached", 34), ("Distress evidence (raw pts)", 9),
    ("Beds", 5), ("Baths", 6), ("SqFt", 7), ("Built", 6), ("Mailing address", 30),
    ("Best tier", 11), ("Dial First #s", 7), ("Phone 1", 15), ("Phone 2", 15), ("Phone 3", 15),
    ("Emails", 6), ("Lead status", 14), ("Touches", 7), ("Owner's other D4D records", 9),
    ("Deal pts", 7), ("Condition pts", 7), ("Investor pts", 7), ("Evidence pts", 7), ("Reach pts", 7),
    ("Why it ranks", 60), ("Must verify", 60), ("CRM link", 44),
]


def _main_row(i, r):
    ph = r["phones"] + [("", "", "")] * 3
    return [
        i, r["opp"], r["deal_size"], r["street"], r["city"], r["zip"], r["neighborhood"], r["county"],
        r["owner"], "Entity" if r["entity"] else "Person", r["age"],
        r["d4d_score"], r["investor_score"], r["realtor_score"], r["on_market_score"],
        r["value"], r["equity_pct"], r["equity_dollars"], r["last_price"],
        r["last_sold"].isoformat() if r["last_sold"] else "", r["years_held"], r["rent"],
        round(r["yield"], 1) if r["yield"] else None, r["ltv"],
        (r["mortgage_type"] or ("None of record" if r["equity_pct"] and r["equity_pct"] >= 99.5 else "")),
        r["tax_owed"], "yes" if r["vacant"] else "",
        ", ".join(l for l in r["lists"] if l != "D4D Top 3000"), r["evidence_raw"],
        r["beds"], r["baths"], r["sqft"], r["year"],
        ", ".join(x for x in [r["mail_street"], r["mail_city"], r["mail_state"], r["mail_zip"]] if x),
        r["reach"], r["n_first"],
        "%s %s %s" % ph[0] if ph[0][0] else "", "%s %s %s" % ph[1] if ph[1][0] else "",
        "%s %s %s" % ph[2] if ph[2][0] else "",
        r["n_emails"], r["status"], r["attempts"], r["owner_other"],
        r["pts"]["deal"], r["pts"]["cond"], r["pts"]["inv"], r["pts"]["evid"], r["pts"]["reach"],
        r["why"], r["verify"], r["crm_url"],
    ]


def _write_table(ws, start, cols, rows, money_idx=(), pct_idx=(), freeze="E{}"):
    _hdr(ws, start, [c[0] for c in cols])
    _widths(ws, [c[1] for c in cols])
    r = start + 1
    for row in rows:
        for j, v in enumerate(row, 1):
            c = ws.cell(row=r, column=j, value=no_dash(v) if isinstance(v, str) else v)
            c.alignment = Alignment(vertical="top", wrap_text=j in (28, 49, 50) and len(cols) == len(MAIN_COLS))
            if j in money_idx and isinstance(v, (int, float)):
                c.number_format = "$#,##0"
            elif j in pct_idx and isinstance(v, (int, float)):
                c.number_format = '0"%"'
            elif isinstance(v, float):
                c.number_format = "0.0"
        r += 1
    ws.freeze_panes = freeze.format(start + 1)
    ws.auto_filter.ref = "A%d:%s%d" % (start, get_column_letter(len(cols)), max(r - 1, start))
    return r


def d4d_words(n_top: int) -> dict:
    """The workbook's words about where its rows came from: the D4D Dream 100's own, exactly as this
    module always wrote them. Another cohort (a list the team ranks with its idle_records workflow)
    passes its own words to write_workbook, because a sheet that says "D4D top 3,000" over a different
    list states a fact that is not true, and the sheet is what a person reads."""
    top = "Dream %d" % n_top
    return {
        "top_label": top,
        "title": "Dream %d: the largest opportunities in the D4D top 3,000" % n_top,
        "subtitle": "Ranked by opportunity score (deal size 35, condition 20, investor 20, evidence 15, reach 10). Built %s. Read-only snapshot of ty+2." % date.today().isoformat(),
        "overview": "One row per property. Three sources joined on the CRM record: the D4D visual distress rank, the record's DataFlik scores and deal fields, and the lists attached in the account.",
        "condition": "The D4D visual distress score from the street pano, ramped 69 (the cohort floor) to 100.",
        "reach": "Best dial tier on the owner's phones after the D4D Trestle scoring: Dial First 10, Second 7, Third 4, Fourth 2. DNC or opt-out scores 0 and is marked mail only.",
        "portfolio_sheet": "Owners holding two or more properties in the D4D 3,000, summed. One conversation can move several doors.",
        "portfolio_title": "Owners with two or more properties in the D4D 3,000",
        "condition_caveat": "The D4D score reads the most recent street pano, which can be years old. Drive the top rows.",
        "phones_caveat": "Tiers are Trestle scores from the 2026-09-11 D4D run. Numbers disposed WRONG, DEAD or DNC were skipped.",
        "cohort_list": "D4D Top 3000",
    }


def write_workbook(top, bench, ranked, excluded, portfolios, path: Path, n_top: int, words: dict | None = None):
    w_ = d4d_words(n_top)
    w_.update(words or {})
    wb = Workbook()
    money = (3, 16, 18, 19, 22, 26)
    pct = (17, 24)

    # 1 Dream 100
    ws = wb.active
    ws.title = w_["top_label"]
    _title(ws, w_["title"], w_["subtitle"])
    _write_table(ws, 4, MAIN_COLS, [_main_row(i, r) for i, r in enumerate(top, 1)], money, pct)

    # 2 Overview
    ov = wb.create_sheet("Overview")
    _title(ov, "How this list was built", w_["overview"])
    sum_deal = sum(r["deal_size"] for r in top)
    sum_val = sum(r["value"] for r in top)
    med = sorted(r["value"] for r in top)[len(top) // 2] if top else 0
    lines = [
        ("Headline", ""),
        ("Properties ranked", len(ranked)),
        ("Excluded before ranking", len(excluded)),
        ("%s combined value" % w_["top_label"], sum_val),
        ("%s combined discount room" % w_["top_label"], sum_deal),
        ("Median value in the %s" % w_["top_label"], med),
        ("Reachable now (Dial First or Second)", sum(1 for r in top if r["best_tier"] in ("Dial First", "Dial Second") and not r["dnc"])),
        ("DNC or opt-out (mail only)", sum(1 for r in top if r["dnc"])),
        ("Already in the pipeline (status set)", sum(1 for r in top if r["status_l"] in WORKED_STATUSES)),
        ("Entity owners", sum(1 for r in top if r["entity"])),
        ("Vacant", sum(1 for r in top if r["vacant"])),
        ("Owner deceased or estate list", sum(1 for r in top if r["deceased"] or {"Obituary", "Probate", "Pre-Probate/Deceased"} & set(r["lists"]))),
        ("", ""),
        ("Biggest single conversations (Portfolio owners sheet)", ""),
    ] + [
        ("%s (%d properties)" % (p["owner"], p["n"]),
         "$%s combined value, $%s discount room, best rank %s, %s" % (
             format(int(p["value"]), ","), format(int(p["deal"]), ","), p["best_rank"] or "none qualified", p["best_reach"] or "no tier"))
        for p in portfolios[:8]
    ] + [
        ("", ""),
        ("Opportunity score, 100 points", ""),
        ("Deal size (35)", "Discount room = the smaller of equity dollars and 30% of value, ramped $15K to $120K. A seller cannot sell below the payoff, and the 70% rule gap is the most a wholesale buyer pays for. This is room, not profit."),
        ("Condition (20)", w_["condition"]),
        ("Investor (20)", "DataFlik investor_score ramped 20 to 90. Up to 5 points off when realtor_score is 70 or higher, because that owner is likely to list with an agent."),
        ("Evidence (15)", "Distress the account can point to: attached lists (Tax Delinquent 4, Vacant 4, Foreclosure 5, Probate/Obituary 4, Liens 3, Tired Landlord 2, Out-of-State 2, Senior/Absentee 1 ...) plus record fields (tax owed, deceased, auction date, probate open, age 75+, held 20+ years). Capped at 14 raw points."),
        ("Reach (10)", w_["reach"]),
        ("", ""),
        ("Gates (see the Excluded sheet)", "Dead lead status (sold, closed, not interested, buyer); tagged recently sold or Do Not Market; on a Dispo buyer list; no value; over the $700K buy box; not single family; upside down; listed on the MLS."),
        ("Not a gate", "Records already being prospected stay in and are flagged on Must verify, because a Dream 100 that hides the team's own hottest files is wrong."),
        ("", ""),
        ("Sheets", ""),
        (w_["top_label"], "The list, one row per property, every input visible."),
        ("Dial sheet", "The same %d with up to five scored numbers each, ready for the dialer." % n_top),
        ("Bench", "Ranks %d to %d, the next in line when a Dream row closes or dies." % (n_top + 1, n_top + 200)),
        ("Portfolio owners", w_["portfolio_sheet"]),
        ("Full ranking", "Every qualified property with its score, for re-cutting by zip, neighborhood or list."),
        ("Excluded", "Every record dropped before ranking, with the reason, so nothing vanishes silently."),
        ("", ""),
        ("Caveats", ""),
        ("Value", "estimate_value is an AVM. Comp every Dream row before an offer."),
        ("Condition", w_["condition_caveat"]),
        ("Equity", "No mortgage of record reads as 100% equity. A private note or a HELOC would not show."),
        ("Lists", "A list is evidence the account attached, not a verified fact. Tax Delinquent and Liens came from county pulls with their own dates."),
        ("Phones", w_["phones_caveat"]),
    ]
    r = 4
    for a, b in lines:
        ov.cell(row=r, column=1, value=a)
        c = ov.cell(row=r, column=2, value=no_dash(b) if isinstance(b, str) else b)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if isinstance(b, (int, float)) and b > 1000:
            c.number_format = "$#,##0" if "value" in a.lower() or "room" in a.lower() else "#,##0"
        if a and b == "":
            ov.cell(row=r, column=1).font = Font(bold=True, color=BLUE)
        r += 1
    _widths(ov, [40, 110])

    # 3 Dial sheet
    ds = wb.create_sheet("Dial sheet")
    _title(ds, "%s dial sheet" % w_["top_label"], "Numbers sorted best tier first, mobiles before landlines. Ask for the owner named; entity rows need the signing member.")
    dcols = [("Rank", 6), ("Opp", 6), ("Owner", 24), ("Ask who", 22), ("Address", 26), ("City", 12), ("Deal size $", 11),
             ("Value", 11), ("Reach", 12), ("Lead status", 12)]
    for k in range(1, 6):
        dcols += [("Phone %d" % k, 15), ("Type %d" % k, 8), ("Tier %d" % k, 11)]
    dcols += [("Opener hook", 50), ("CRM link", 44)]
    drows = []
    for i, r in enumerate(top, 1):
        hook = []
        if r["vacant"]:
            hook.append("the house looks empty")
        if r["d4d_score"] and r["d4d_score"] >= 90:
            hook.append("it could use some work from the street")
        if r["absentee"]:
            hook.append("you're not at the property")
        if r["years_held"] and r["years_held"] >= 20:
            hook.append("you've had it %d years" % int(r["years_held"]))
        if "Tired Landlord" in r["lists"]:
            hook.append("rental you might be done with")
        ask = r["owner_first"] if (r["owner_first"] and not r["entity"]) else ("signing member of " + r["owner"] if r["entity"] else "owner of the address")
        row = [i, r["opp"], r["owner"], ask, r["street"], r["city"], r["deal_size"], r["value"], r["reach"], r["status"]]
        ph = r["phones"] + [("", "", "")] * 5
        for k in range(5):
            row += list(ph[k])
        row += ["; ".join(hook) or "owner of the address, no visible hook", r["crm_url"]]
        drows.append(row)
    _write_table(ds, 4, dcols, drows, money_idx=(7, 8))

    # 4 Bench
    bs = wb.create_sheet("Bench")
    _title(bs, "Bench: ranks %d to %d" % (n_top + 1, n_top + len(bench)), "Next in line. Same columns as the Dream sheet.")
    _write_table(bs, 4, MAIN_COLS, [_main_row(n_top + i, r) for i, r in enumerate(bench, 1)], money, pct)

    # 5 Portfolio owners
    ps = wb.create_sheet("Portfolio owners")
    _title(ps, w_["portfolio_title"], "Grouped by owner record and mailing address. Summed discount room is the size of the conversation.")
    pcols = [("Owner", 28), ("Owner type", 9), ("Mailing address", 34), ("Properties", 8), ("Combined value", 13),
             ("Combined discount room", 13), ("Best opp score", 8), ("Best rank", 8), ("Best reach", 12),
             ("Addresses", 70), ("Lists seen", 40)]
    prow = []
    for p in portfolios:
        prow.append([p["owner"], "Entity" if p["entity"] else "Person", p["mail"], p["n"], p["value"], p["deal"],
                     p["best_opp"], p["best_rank"], p["best_reach"], "; ".join(p["addresses"]), ", ".join(sorted(p["lists"]))])
    _write_table(ps, 4, pcols, prow, money_idx=(5, 6), freeze="B{}")

    # 6 Full ranking
    fs = wb.create_sheet("Full ranking")
    _title(fs, "Every qualified property, ranked", "%d rows. Filter by zip, neighborhood, list or tier to re-cut the list." % len(ranked))
    _write_table(fs, 4, MAIN_COLS, [_main_row(i, r) for i, r in enumerate(ranked, 1)], money, pct)

    # 7 Excluded
    xs = wb.create_sheet("Excluded")
    _title(xs, "Excluded before ranking", "%d records. Nothing is dropped silently." % len(excluded))
    reasons = Counter(x["reason"] for x in excluded)
    r = 4
    xs.cell(row=r, column=1, value="Reason").font = Font(bold=True, color=BLUE)
    xs.cell(row=r, column=2, value="Records").font = Font(bold=True, color=BLUE)
    r += 1
    for k, n in reasons.most_common():
        xs.cell(row=r, column=1, value=k); xs.cell(row=r, column=2, value=n); r += 1
    r += 1
    xcols = [("Reason", 40), ("D4D rank", 8), ("Address", 26), ("City", 12), ("Owner", 24), ("Value", 11),
             ("Equity %", 8), ("Lead status", 14), ("Lists", 40), ("CRM link", 44)]
    xrows = [[x["reason"], x["d4d_rank"], x["street"], x["city"], x["owner"], x["value"], x["equity_pct"], x["status"],
              ", ".join(l for l in x["lists"] if l != w_["cohort_list"]), x["crm_url"]] for x in excluded]
    _write_table(xs, r, xcols, xrows, money_idx=(6,), pct_idx=(7,), freeze="A{}")

    for w in wb.worksheets:
        w.sheet_view.zoomScale = 90
    tmp = path.with_name("_PENDING_" + path.name)
    wb.save(tmp)
    tmp.replace(path)


# ------------------------------------------------------------- build --

def phase_build(args) -> None:
    cohort = {}
    for r in json.loads(d4d.COHORT_PATH.read_text(encoding="utf-8")):
        u = r.get("saved_uuid")
        if u and (u not in cohort or (r.get("rank") or 9999) < (cohort[u].get("rank") or 9999)):
            cohort[u] = r  # SiftMap holds two ids for one address; keep the better rank
    full = json.loads(FULL_PATH.read_text(encoding="utf-8"))
    scores = json.loads(d4d.SCORES_PATH.read_text(encoding="utf-8")) if d4d.SCORES_PATH.exists() else {}
    if not full:
        raise SystemExit("no full records; run --phase pull first")
    ranked, excluded, portfolios = rank_records(full, cohort, scores)

    n = args.top
    top, bench = ranked[:n], ranked[n:n + 200]
    stamp = date.today().strftime("%Y%m%d")
    xlsx = OUT / ("Dream100_Knox_Blount_%s.xlsx" % stamp)
    write_workbook(top, bench, ranked, excluded, portfolios, xlsx, n)

    # CSV twins
    with open(OUT / ("Dream100_%s.csv" % stamp), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([c[0] for c in MAIN_COLS])
        for i, r in enumerate(top, 1):
            w.writerow([no_dash(v) if isinstance(v, str) else v for v in _main_row(i, r)])
    (OUT / "dream100_ranked.json").write_text(json.dumps(
        [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()} for r in ranked],
        indent=0, default=str), encoding="utf-8")

    print("build: %d qualified, %d excluded, %d portfolio owners" % (len(ranked), len(excluded), len(portfolios)))
    print("Dream %d: combined value $%s, combined discount room $%s" % (
        n, format(int(sum(r['value'] for r in top)), ","), format(int(sum(r['deal_size'] for r in top)), ",")))
    print("  reachable (Dial First/Second, not DNC): %d; entity owners: %d; already worked: %d" % (
        sum(1 for r in top if r["best_tier"] in ("Dial First", "Dial Second") and not r["dnc"]),
        sum(1 for r in top if r["entity"]), sum(1 for r in top if r["status_l"] in WORKED_STATUSES)))
    print("wrote", xlsx)
    return ranked, excluded, top


def rank_records(full: dict, cohort: dict, scores: dict) -> tuple[list, list, list]:
    """(ranked, excluded, portfolios) for a set of full CRM records: every record flattened, gated and
    scored, and the owners holding two or more of them summed. The D4D build and the team's
    idle_records workflow both rank through here, so a list ranked on the agents box is ranked by
    exactly the rules the workstation's Dream 100 uses. `cohort` maps a record uuid to its D4D row
    (rank and visual distress score); a record with none scores no condition points."""
    rows, excluded = [], []
    for u, rec in full.items():
        co = cohort.get(u) or {}
        r = flatten(rec, co, scores)
        why = gate_reason(r)
        if why:
            r["reason"] = why
            excluded.append(r)
        else:
            rows.append(score(r))

    # owner portfolios across the whole cohort (excluded rows count too: an owner is an owner)
    groups = defaultdict(list)
    for r in rows + excluded:
        key = r["owner_uuid"] or (r["owner"].lower(), r["mail_street"].lower())
        if r["owner"]:
            groups[key].append(r)
    other = {}
    portfolios = []
    for key, g in groups.items():
        for r in g:
            other[r["uuid"]] = len(g) - 1
        if len(g) >= 2:
            live = [x for x in g if "opp" in x]
            portfolios.append({
                "owner": g[0]["owner"], "entity": g[0]["entity"],
                "mail": ", ".join(x for x in [g[0]["mail_street"], g[0]["mail_city"], g[0]["mail_state"]] if x),
                "n": len(g), "value": sum(x["value"] or 0 for x in g),
                "deal": sum(x.get("deal_size", 0) or 0 for x in g),
                "best_opp": max((x["opp"] for x in live), default=None),
                "best_rank": None, "best_reach": max((x["reach"] for x in live), default="", key=lambda t: TIER_PTS.get(t, -1)),
                "addresses": [x["street"] for x in g], "lists": {l for x in g for l in x["lists"] if l not in NON_EVIDENCE},
                "uuids": [x["uuid"] for x in live],
            })
    for r in rows + excluded:
        r["owner_other"] = other.get(r["uuid"], 0)

    ranked = sorted(rows, key=lambda r: (-r["opp"], -r["deal_size"], r["d4d_rank"] or 9999))
    rank_of = {r["uuid"]: i for i, r in enumerate(ranked, 1)}
    for p in portfolios:
        p["best_rank"] = min((rank_of[u] for u in p["uuids"] if u in rank_of), default=None)
    portfolios.sort(key=lambda p: (-p["deal"], -p["n"]))
    excluded.sort(key=lambda r: (r["reason"], r["d4d_rank"] or 9999))
    return ranked, excluded, portfolios


def csv_rows(top: list) -> list[list]:
    """The CSV twin of the top sheet: the header, then one dash-free row per property."""
    out = [[c[0] for c in MAIN_COLS]]
    for i, r in enumerate(top, 1):
        out.append([no_dash(v) if isinstance(v, str) else v for v in _main_row(i, r)])
    return out
