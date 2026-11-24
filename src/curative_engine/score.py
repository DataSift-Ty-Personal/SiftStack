"""The score: how likely each house is to be a curative title deal, and what to do next.

A curative title deal is a house whose owner of record died, whose title is still in that name, where nobody
opened a probate, with taxes or liens behind and enough equity to pay every heir their share. The family has to
be researched before anyone calls, and every heir with a share signs.

105 points, shown out of 100:
    Death and title (35)   the owner died (by how sure we are), the title still in the decedent's name or an
                           estate on the roll, one owner on the deed (a second owner may have survived)
    No probate (20)        no estate opened and the death at least six months back (before six months the family
                           is still grieving and sorting things out: no calls yet)
    Taxes and liens (20)   county and city years behind, turned over for collection or a tax suit, $2,000 or more
                           owed, liens, and a foreclosure sale ahead (the hardest deadline when paired with a death)
    Money (20)             value band, how many heirs the equity carries at $50,000 each, free and clear
    Disconnected (10)      absentee or out-of-state mail, a vacant house, family other than the owner paying bills
    Penalties              a trust, a life estate, a spouse trap, a surviving co-owner, research that found the
                           owner living, an earlier not interested

Tier 1 (work now): score 70+, the death confirmed, a ghost, magic three or an estate in foreclosure, equity for at
least one $50,000 share, no trust, no spouse trap, no surviving co-owner. An heir story, once written, overrides
the tier: it settles who inherits and what each share is worth.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date

from . import common as C
from .counties import county_for
from .deaths import index_rows, match_person

SHARE_FLOOR = 50000          # equity each heir's share must clear for the house to be worth buying share by share
VALUE_FLOOR = 150000


def _plays():
    return ("Ghost property", "Magic three: one owner, estate, taxes behind", "Estate in foreclosure")


def score_row(r: dict, obits: dict | None, fam: dict | None = None, probate: dict | None = None,
              stories: dict | None = None, today: date | None = None) -> dict:
    today = today or C.today()
    notes, flags, verify = [], [], []
    deed = r.get("deed") or []
    n_owners = max(r.get("deed_count") or 0, len(r.get("roll_people") or []), 1)
    ms = r.get("months_since_death")
    conf = r.get("death_conf") or 0
    survivor_risk: bool | str = False
    spouse_trap = False
    checks = []
    owner_age = r.get("owner_age")
    obit_day = C.parse_day(r.get("obit_day"))
    last_sale = C.parse_day(r.get("last_sale"))
    death_day = C.parse_day(r.get("death_day"))
    cty = county_for(fips=r.get("fips") or "")
    state = r.get("state") or cty.state or "TN"
    state_name = C.STATE_NAMES.get(state, state)

    # ---- the obituary index check (owners 1 and 2)
    if obits is not None and not r.get("decedent_court") and not r.get("research_status"):
        for i, person in enumerate(deed[:2]):
            t = C.name_toks(person)
            if len(t) < 2:
                continue
            txt = obits.get("%s|%s|%s" % (r.get("fips") or "", t[-1], t[0]))
            if txt is None:
                continue
            # the county roll carries the generation the deed drops: carry it into the match
            roll_words = set(re.findall(r"[A-Z]+", (r.get("roll_owner") or "").upper()))
            gen = sorted(roll_words & C.GENERATIONS) if {t[-1], t[0]} <= roll_words else []
            verdict, row = match_person(person + (" " + " ".join(gen) if gen else ""), index_rows(txt),
                                        owner_age if i == 0 else None, obit_day, last_sale, today)
            checks.append((person, verdict, row))
        weight = {"confirmed": 3, "likely": 2}
        conf_people = [c for c in checks if c[1] in weight]
        if checks:
            if len(deed) == 1 and conf_people:
                conf = max(conf, weight[conf_people[0][1]])
            elif len(deed) >= 2:
                if len(conf_people) >= 2:
                    conf = max(conf, min(weight[c[1]] for c in conf_people))
                elif conf_people:
                    who = conf_people[0][0]
                    if who == deed[0] or len(deed) == 1:
                        conf = max(conf, weight[conf_people[0][1]])
                    survivor_risk = "one"
                    if who != deed[0] and (r.get("death_conf") or 0) <= 1:
                        spouse_trap = True
            for _p, v, row in checks:
                if v in weight and row and row["day"]:
                    if death_day is None or row["day"] > death_day:
                        death_day = row["day"]
        if death_day:
            ms = round((today - death_day).days / 30.44, 1)

    # ---- SmartSkip: a deceased flag on a title holder settles the death
    ss_dead, ss_both = [], False
    if fam:
        ss_dead = [s["name"] for s in fam.get("subjects") or [] if s.get("deceased")]
        if ss_dead:
            conf = max(conf, 3)
            r = dict(r, evidence=list(r.get("evidence") or []) + ["SmartSkip marks %s deceased" % ", ".join(ss_dead)])
            if len(fam.get("subjects") or []) >= 2:
                if fam.get("all_deceased"):
                    ss_both, survivor_risk, spouse_trap = True, False, False
                elif survivor_risk is False:
                    survivor_risk = "one"

    # ---- research that names every title holder dead settles the second death the same way
    decs = r.get("decedents") or []
    if len(deed) >= 2 and decs and all(any(C.same_person(d, x) for x in decs) for d in deed):
        ss_both, survivor_risk, spouse_trap = True, False, False
        conf = max(conf, 3)

    # ---- the probate court's index, where the county has one: a will probated or an estate open puts the house
    # on the probate path; a closed no-will estate is a note (the heirs still hold their shares).
    stories = stories or {}
    story = stories.get(r.get("street"))
    pidx = pidx_note = unsure = None
    pidx_searched = False
    pidx_text = ""
    index = (probate or {}).get(r.get("fips") or "")
    if index is not None and index.available() and not r.get("probate_open"):
        index._load()
        days = {p: row["day"] for p, v, row in checks if v in ("confirmed", "likely") and row and row.get("day")}
        found, last = [], None
        for person in deed[:3]:
            died = days.get(person) or (death_day if len(deed) == 1 else None)
            if died:
                last = max(last, died) if last else died
                if index.covers_to:
                    pidx_searched = pidx_searched or died < index.covers_to
                found += [c for c in index.lookup(person, died) if c["fit"] != "weak"]
        if last:    # only the estate of the owner who died last holds the house: a first spouse's will is history
            cut = date.fromordinal(last.toordinal() - 45).isoformat()
            found = [c for c in found if c["opened"] >= cut]
        for c in sorted(found, key=lambda c: c["opened"], reverse=True):
            if c["kind"] in ("will", "open") and c["sure"]:
                pidx = c
                break
            if c["kind"] in ("will", "open") and not unsure:
                unsure = c
            elif c["kind"] == "closed" and not pidx_note:
                pidx_note = c
        label = cty.probate_name or "the probate index"
        if pidx and not story:
            r = dict(r, probate_open=True, probate_month=pidx["opened"][:7], probate_src="index",
                     probate_closed=pidx["kind"] == "will" and pidx.get("status") == "CLOSED")
            notes.append("%s: %s" % (label, index.describe(pidx)))
        elif not story:
            if unsure:
                notes.append("%s: a case under the same name, %s; the name is common in the index" % (label, index.describe(unsure)))
                verify.append("probate case %s: check it is the owner's estate (same name, opened %s)" % (unsure["docket"], unsure["open"]))
            if pidx_note:
                notes.append("%s: %s; the court file names the heirs" % (label, index.describe(pidx_note)))
                verify.append("the closed estate file %s: it names the heirs and shows whether the house was sold" % pidx_note["docket"])
        through = index.covers_to.strftime("%b %Y") if index.covers_to else "its last case"
        pidx_text = (index.describe(pidx) if pidx else index.describe(pidx_note) if pidx_note
                     else ("same name, check: " + index.describe(unsure)) if unsure
                     else ("none found through %s" % through) if pidx_searched else "")
        if story and pidx_text:
            pidx_text += " (the heir story decides)"

    # ---- A. the title holder died and the title is stuck (max 35)
    A1 = {3: 20, 2: 14, 1: 8, 0: 0}[conf]
    roll = r.get("roll_owner") or ""
    if r.get("estate_named"):
        A2 = 10
        notes.append("the county roll carries the owner as an estate or heirs")
    elif r.get("title_holder_dead") is True:
        A2 = 10
        notes.append("still in the decedent's name on the county roll")
    elif r.get("title_holder_dead") is False and r.get("decedent_court"):
        A2 = -12
        flags.append("the county roll no longer shows the decedent: title may have moved")
    elif roll and deed and any(set(C.toks(d)) & set(C.toks(roll)) for d in deed):
        A2 = 6
        notes.append("the roll still shows the same owner of record")
    elif roll:
        A2 = -4
        flags.append("the roll owner (%s) differs from the deed owner on file" % roll)
    else:
        A2 = 0
    if n_owners <= 1:
        A3 = 5
    elif n_owners == 2:
        A3 = 5 if (ss_both or len([c for c in checks if c[1] in ("confirmed", "likely")]) >= 2) else -2
        if A3 < 0 and not survivor_risk:
            survivor_risk = "unknown"
    else:
        A3 = 3
        notes.append("%d names on the title already" % n_owners)
    A = max(0, min(35, A1 + A2 + A3))

    # ---- B. no probate (max 20)
    if not r.get("probate_open"):
        B = 12 if ms is None else (20 if ms >= 6 else (13 if ms >= 4 else 5))
    else:
        stalled = False
        if r.get("probate_month"):
            y, m = str(r["probate_month"]).split("-")[:2]
            age_m = (today.year - int(y)) * 12 + today.month - int(m)
            stalled = bool(age_m >= 10 and r.get("title_holder_dead") is True and (r.get("tax_years") or [])
                           and not r.get("probate_closed"))     # a will probated and closed is done, not stalled
        B = 10 if stalled else 3
        if stalled:
            notes.append("probate opened %s and the house is still in the decedent's name with taxes behind" % r["probate_month"])

    # ---- C. taxes and liens behind (max 20)
    all_years = sorted(set(r.get("tax_years") or []) | set(r.get("city_years") or []))
    years = len(all_years)
    tax_total = (r.get("tax_amount") or 0) + (r.get("city_amount") or 0)
    C_ = {0: 0, 1: 7, 2: 11}.get(years, 14)
    if r.get("turned_over") or r.get("city_suit"):
        C_ += 3
    if tax_total >= 2000:
        C_ += 2
    lien_any = bool(r.get("lien_flags")) or (r.get("crm_lien") or 0) > 0
    if lien_any:
        C_ += 2
    if r.get("fc_ahead"):
        C_ += 12 if conf >= 2 else 4
    C_ = min(C_, 20)

    # ---- D. money (max 20)
    v = r.get("avm") or 0
    if v < 100000:
        band = 0
    elif v < VALUE_FLOOR:
        band = 3
    elif v < 250000:
        band = 7
    elif v <= 350000:
        band = 10
    elif v <= 500000:
        band = 8
    elif v <= 750000:
        band = 6
    else:
        band = 2
    carry = r.get("carry") or 0
    Dm = band + min(carry, 4) * 2 + (2 if r.get("free_clear") else 0)
    if r.get("equity") is None or (r.get("equity") or 0) <= 0:
        Dm = min(Dm, band)
    Dm = min(Dm, 20)

    # ---- E. disconnection (max 10)
    E = (3 if r.get("absentee") else 0) + (2 if r.get("oos") else 0) + (3 if r.get("vacant") else 0)
    if r.get("payor_kind") == "family (same surname)":
        E += 2
    E = min(E, 10)

    # ---- penalties
    P = 0
    if r.get("life_estate"):
        P += 10
        flags.append("a life estate on the title: when the life tenant dies the remainder owners take it; find out who they are")
    if r.get("trust"):
        P += 15
        flags.append("held in a trust: a successor trustee can usually sell, so it may not be stuck")
    if "not_interested" in (r.get("status") or "") or "not interested" in (r.get("status") or ""):
        P += 5
        flags.append("someone on the record already said not interested")
    if str(r.get("research_status") or "").lower() == "living" and not r.get("decedent_court"):
        P += 20
        flags.append("our research found the owner living")
    if spouse_trap:
        P += 18
        flags.append("spouse trap: the obituary found is the co-owner's, not the first owner's")
    elif survivor_risk == "one":
        P += 12
        flags.append("two owners on the deed and only one death found: the survivor most likely holds it by survivorship")
    elif survivor_risk:
        P += 6
        flags.append("two owners on the deed: a surviving co-owner usually takes it all (verify both deaths)")
    total = A + B + C_ + Dm + E - P
    score = max(0, round(total * 100 / 105))

    # ---- the play and the tier
    ghost = conf >= 2 and not r.get("probate_open") and (ms is None or ms >= 6)
    if conf <= 0:
        play = "No death signal (foreclosure only)" if r.get("in_fc") else "No death signal"
    elif r.get("probate_open"):
        play = "Probate open (stalled)" if B >= 10 else "Probate open: a probate deal"
    elif conf == 1:
        play = "Possible death: verify first"
    elif ms is not None and ms < 6:
        play = "Too early (death under 6 months)"
    elif r.get("in_fc") and (r.get("fc_ahead") or r.get("sale_day")):
        play = "Estate in foreclosure"
    elif ghost and (n_owners <= 1 or ss_both or A3 == 5) and years >= 1:
        play = "Magic three: one owner, estate, taxes behind"
    elif ghost:
        play = "Ghost property"
    else:
        play = "Possible death: verify first"
    disq = r.get("gate") or ""
    if disq == "no sign the owner died" and conf >= 2:
        disq = ""
    mort = r.get("mortgage") or 0
    loan_doubt = bool(v and mort > 1.15 * v)
    if loan_doubt:
        flags.append("the loan estimate (%s) is more than the house is worth: likely an old loan never released; verify the payoff" % C.dollars(mort))
    if not disq and not v:
        disq = "no value on file"
    appr = r.get("appraised") or 0
    sqft = r.get("sqft") or 0
    if not disq and v and appr and appr < 0.10 * v:
        disq = "county appraisal under 10% of the value estimate (likely a vacant lot or a teardown)"
    if not disq and v and not sqft and not r.get("year_built"):
        disq = "no living area or year built on file (likely a vacant lot)"
    if not disq and v and v < VALUE_FLOOR:
        disq = "under the $150,000 value floor"
    if not disq and r.get("equity") is not None and (r.get("equity") or 0) < SHARE_FLOOR and conf >= 1 and not loan_doubt:
        disq = "under $50,000 of equity"
    if disq:
        tier = "Out"
    elif (score >= 70 and conf >= 3 and play in _plays() and carry >= 1 and not spouse_trap and not survivor_risk
          and not r.get("trust") and not r.get("life_estate")):
        tier = "1 Work now"
    elif score >= 55 and conf >= 1 and not spouse_trap:
        tier = "2 Research first"
    else:
        tier = "3 Watch"

    # ---- the heir story, where the research has one: it settles who inherits and what each share is worth
    if story:
        sv = str(story.get("verdict") or "")
        if sv.startswith("Not curative"):
            disq, tier = "the heir research found the owner alive", "Out"
        elif sv.startswith("Not a house"):
            disq, tier = "county appraisal far under the value estimate (heir research: a lot or a teardown)", "Out"
        elif sv.startswith("Under $50K"):
            disq, tier = "under $50,000 of equity per heir (heir research)", "Out"
        elif sv == "Will or probate on file":
            play = "Probate open: a probate deal"
            if tier == "1 Work now":
                tier = "2 Research first"          # a probate deal, not a curative one: it goes to the probate tab
            notes.append("the heir research found a will or a probate case")
        elif sv.startswith("Work now") and not disq:
            tier = "1 Work now"
        elif sv == "Research more" and tier != "Out":
            tier = "2 Research first"

    # ---- why it ranks here
    why = []
    dec_name = (r.get("decedent_court") or (checks[0][0] if checks and checks[0][1] in ("confirmed", "likely") else "")
                or (deed[0] if deed else ""))
    conf_txt = {3: "confirmed", 2: "likely", 1: "possible", 0: "no sign"}[conf]
    src = []
    for _p, vv, row in checks:
        if vv in ("confirmed", "likely") and row:
            src.append("obituary index: %s, age %s, obituary %s%s" % (
                row["given"] + " " + row["last"], row["age"] or "?",
                row["day"].strftime("%b %d, %Y") if row["day"] else "?",
                "" if vv == "confirmed" else ", the age fits but the index has more than one person by that name"))
    if any(vv == "likely" for _, vv, _ in checks):
        verify.append("which of the same-name obituaries is the owner's (the age fits one; confirm with the family or the obituary text)")
    ev = "; ".join(src or r.get("evidence") or [])
    if conf >= 1:
        why.append("Owner death %s (%s)" % (conf_txt, ev[:160]))
    if death_day:
        why.append("died about %s (%s months ago)" % (death_day.strftime("%b %Y"), int(round(ms or 0))))
        if ms is not None and ms < 6 and not r.get("probate_open"):
            mo = death_day.month + 6
            ready = date(death_day.year + (mo - 1) // 12, (mo - 1) % 12 + 1, 1)
            why.append("six months pass about %s (no calls before then)" % ready.strftime("%b %Y"))
    if r.get("probate_src") == "index" and pidx:
        why.append("probate case on file: " + index.describe(pidx))
    elif r.get("probate_open"):
        why.append("probate on file%s" % (" (" + str(r["probate_month"]) + ")" if r.get("probate_month") else ""))
    elif conf >= 1:
        why.append("no probate found")
    if r.get("tax_years"):
        why.append("county tax behind %s (%s%s)" % (", ".join(r["tax_years"]), C.dollars(r.get("tax_amount")),
                                                    ", turned over for collection" if r.get("turned_over") else ""))
    elif r.get("tax_status") == "ok":
        why.append("county tax current")
    if r.get("city_years"):
        why.append("city tax behind %s (%s%s)" % (", ".join(r["city_years"]), C.dollars(r.get("city_amount")),
                                                  ", tax suit costs on the bill" if r.get("city_suit") else ""))
    elif r.get("city_read"):
        why.append("city tax current")
    if lien_any:
        la = r.get("lien_amount") or 0
        why.append("liens %s" % (C.dollars(la) if la else ", ".join(r.get("lien_flags") or ["on record"])))
    if r.get("fc_ahead"):
        why.append("foreclosure sale %s" % r["sale_day"])
    why.append("value %s, equity %s%s" % (C.dollars(v) if v else "?", C.dollars(r.get("equity")) if r.get("equity") is not None else "?",
                                           ", free and clear" if r.get("free_clear") else ""))
    if r.get("vacant"):
        why.append("reads vacant")
    if r.get("oos"):
        why.append("mailing out of state (%s)" % r.get("mail_state"))
    if r.get("payor_kind") == "family (same surname)":
        why.append("last tax bill paid by %s (family)" % r.get("last_payor"))
    if r.get("payor_kind") == "someone else":
        flags.append("the last county bill was paid by %s, not the owner: a tenant, a buyer on contract or an in-law; ask who" % C.nice(r.get("last_payor")))
    if r.get("partial_payor"):
        why.append("part of a late bill paid by %s" % str(r["partial_payor"]).title())
        flags.append("someone is paying down the late county bill (%s): not abandoned" % str(r["partial_payor"]).title())
    why += notes
    heirs_n = len(r.get("dms") or [])
    if fam:
        if fam.get("heirs_n") and not heirs_n:
            heirs_n = fam["heirs_n"]
        if fam.get("heirs"):
            more = (" and %d more" % (len(fam["heirs"]) - 6)) if len(fam["heirs"]) > 6 else ""
            why.append("SmartSkip family: %s%s (%s)" % (", ".join(fam["heirs"][:6]), more, fam.get("heir_order")))
        sp = [x for x in fam.get("relatives") or [] if x.get("rel") in ("Spouse", "Husband", "Wife")]
        if sp and n_owners <= 1 and conf >= 2:
            flags.append("SmartSkip lists a spouse (%s, age %s): if living, a spouse usually takes a share; confirm"
                         % (sp[0]["name"], sp[0].get("age") or "?"))
        if heirs_n and (r.get("equity") or 0) / heirs_n < SHARE_FLOOR:
            flags.append("equity per likely heir is about %s, under the $50,000 a share floor"
                         % C.dollars((r.get("equity") or 0) / heirs_n))
    elif not r.get("dms"):
        verify.append("the family tree (not run through SmartSkip yet)")

    verify.append("who inherits: a will, the heirs and each share (%s law, with your attorney)" % state_name)
    if n_owners >= 2 or survivor_risk:
        verify.append("both deaths and their order: a married couple's deed usually passes to the survivor")
    if r.get("in_city") and not r.get("city_read"):
        verify.append("city taxes (inside a city, but the city's bill was not read)")
    verify.append("a title search for liens and judgments before the first share is bought")
    if not r.get("probate_open") and death_day:
        if pidx_searched and index is not None and index.covers_to:
            verify.append("the probate court for a case opened since %s (%s shows none through then)"
                          % (index.covers_to.strftime("%b %Y"), cty.probate_name or "the index"))
        else:
            verify.append("the probate court for a case opened after the %d death" % death_day.year)
    if conf <= 2:
        verify.append("the death itself (the obituary and the owner's age)")
    if not r.get("vacant") and not r.get("absentee"):
        verify.append("whether a family member lives there (a non-seller living in it is a walk-away)")

    r2 = dict(r)
    r2.update({"score": score, "tier": tier, "play": play, "disq": disq, "conf2": conf, "conf_txt": conf_txt,
               "months_since_death2": ms, "death_day2": C.iso(death_day),
               "index_checks": [{"person": p, "verdict": vv, "row": (row or {}).get("text")} for p, vv, row in checks],
               "survivor_risk": survivor_risk, "spouse_trap": spouse_trap, "why": "; ".join(why), "flags": flags,
               "verify": verify, "parts": {"death_title": A, "no_probate": B, "taxes_liens": C_, "money": Dm,
                                           "disconnected": E, "penalty": P},
               "decedent_name": dec_name, "n_owners": n_owners, "probate_index_case": pidx_text,
               "known_heirs": heirs_n, "all_tax_years": all_years, "tax_total": round(tax_total, 2),
               "per_heir_ok": (("yes" if (r.get("equity") or 0) / max(heirs_n, 1) >= SHARE_FLOOR else "no") if heirs_n else ""),
               "per_heir": round((r.get("equity") or 0) / heirs_n) if heirs_n else None,
               "ss_dead": ss_dead, "ss_heirs": (fam or {}).get("heirs") or [], "ss_heir_order": (fam or {}).get("heir_order") or "",
               "ss_relatives": len((fam or {}).get("relatives") or []), "ss_with_phones": (fam or {}).get("with_phones") or 0,
               "ss_run": bool(fam),
               "research_done": "yes" if ({"Deep Prospected", "Heirs Resolved"} & set(r.get("tags") or [])) or r.get("research_status") else "",
               "story_verdict": (story or {}).get("verdict") or "", "story_heirs": "; ".join((story or {}).get("heir_list") or []),
               "story_per_heir": (story or {}).get("equity_per_heir"),
               "story_reach": ("%s of %s" % (story["reach"], story["heirs"])) if story and story.get("reach") is not None else "",
               "story_open": "; ".join((story or {}).get("open") or []), "story_file": (story or {}).get("story") or "",
               "story_conf": (story or {}).get("confidence") or "",
               "story_first": 0 if story and str(story.get("verdict", "")).startswith("Work now") else 1})
    if story and story.get("heirs"):
        r2["known_heirs"] = story["heirs"]
        if story.get("equity_per_heir") is not None:
            r2["per_heir"] = story["equity_per_heir"]
            r2["per_heir_ok"] = "yes" if story["equity_per_heir"] >= SHARE_FLOOR else "no"
    return r2


def order_key(r: dict):
    return (r["tier"] == "Out", r["tier"], r.get("story_first", 1), -r["score"], -(r.get("equity") or 0))


def run(ws, refresh_probate: bool = False, out=print) -> list[dict]:
    from . import stories as S
    rows = ws.read("joined.json", []) or []
    obits = ws.read("obits.json")
    family = ws.read("family.json", {}) or {}
    probate = {}
    for fips in sorted({r.get("fips") or "" for r in rows}):
        cty = county_for(fips=fips)
        if cty.probate_sheet:
            probate[fips] = cty.probate_index(ws.work, refresh=refresh_probate)
    stories = S.load(ws)
    today = C.today()
    scored = [score_row(r, obits, family.get(r["key"]), probate, stories, today) for r in rows]
    scored.sort(key=order_key)
    ws.write("scored.json", scored, indent=None)
    out("scored %d | tiers %s" % (len(scored), dict(Counter(r["tier"] for r in scored))))
    out("plays %s" % dict(Counter(r["play"] for r in scored if r["tier"] != "Out").most_common()))
    out("ruled out %s" % dict(Counter(r["disq"] for r in scored if r["tier"] == "Out").most_common(8)))
    return scored
