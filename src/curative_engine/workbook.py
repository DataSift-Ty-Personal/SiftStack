"""The ranked workbook: Overview, Work list (tiers 1 and 2), All ranked, Taxes behind, Probate deals, Foreclosures
with no death, Family trees, Ruled out, Sources and method. It carries owner and family names: keep it inside the
team. Writes output/curative_title/<market>/<Market>_Curative_Title_<date>.xlsx (a copy open in Excel is never
overwritten: the next version is written beside it).
"""
from __future__ import annotations

import os
import sys
from collections import Counter

from . import common as C
from .counties import county_for

if str(C.SRC) not in sys.path:
    sys.path.insert(0, str(C.SRC))

NAVY, BLUE, GREEN, GOLD, GRAY, LINE = "0A1130", "316AFF", "1B9E5A", "8A6D00", "5B6474", "D0D5DD"
MONEY = '"$"#,##0'

WEIGHTS = [
    ("Death and title (35)", "The owner of record died (a court notice, research, a skip trace flag, the obituary index, an "
                             "estate name on the roll, SiftMap's flags), the roll still shows the decedent or an estate, and "
                             "one owner on the deed. A second owner who may have survived costs points."),
    ("No probate (20)", "No estate opened and the death at least 6 months back. An open probate scores 3, or 10 when it "
                        "has stalled: open 10+ months, still in the decedent's name, taxes behind."),
    ("Taxes and liens (20)", "County and city years behind (1 year 7, 2 years 11, 3+ 14), turned over for collection or "
                             "a tax suit, $2,000+ owed, liens, a foreclosure sale ahead (12 when paired with a death)."),
    ("Money (20)", "Value band ($150,000 floor, $250,000 to $350,000 best), how many heirs the equity carries at $50,000 "
                   "each, free and clear."),
    ("Disconnected owners (10)", "Absentee or out-of-state mail, a vacant house, family other than the owner paying bills."),
    ("Penalties", "A trust (a successor trustee can sell), a life estate, a spouse trap, a surviving co-owner, an earlier "
                  "not interested, research that found the owner living."),
    ("Tier 1", "Score 70+, the death confirmed, a ghost, magic three or estate in foreclosure, equity for at least one "
               "$50,000 share, no trust, no spouse trap, no surviving co-owner. An heir story overrides the tier."),
]
GUARDRAILS = [
    "Nobody here is a lawyer. An affidavit of heirship or opening the estate is the attorney's call.",
    "A married couple's deed usually passes to the survivor: the house is stuck only after the second death. Get both dates.",
    "With no will, the state's intestacy law decides the shares (in Tennessee: the children share equally, a child who "
    "died first passes the share to their own children, and a spouse takes the larger of one third or a child's share).",
    "A disclaimer sends a share down the family line: use a deed plus a release, never a disclaimer.",
    "Never imply you represent the estate, the court, the lender or the family. Never promise to stop or delay a foreclosure.",
    "Every share has to pay on its own: start offers at $500 to $1,000 and never pay more than the share is worth to you alone.",
    "Call 1 is discovery with no price; the offer comes on call 2. Walk away when an owner who will not sell lives in the house.",
    "A family member is never a wrong number, and every stop request holds on every channel.",
]
CAVEATS = [
    "Value is the estimate on the record. Price from comps before any offer.",
    "Heirs are known only where research named them. Elsewhere 'Heirs it carries' is equity divided by $50,000.",
    "Liens are what SiftMap flags and the record's own lien field. Older judgments and liens need a title search "
    "before the first share is bought.",
    "Every death that rests on a name match carries the spouse trap; the obituary index check catches most of it, not all.",
    "Read only: nothing in DataSift was changed and nothing was sent.",
]

RANK_COLS = [
    ("Rank", 6), ("Score", 7), ("Tier", 14), ("Play", 24), ("Heir story", 16), ("Heirs from the heir research", 40),
    ("Equity per heir (research)", 11), ("Heirs with a confirmed number", 9), ("Open questions (research)", 44),
    ("Property", 26), ("City", 12), ("ZIP", 7), ("County", 10), ("DataSift record", 12),
    ("Owner of record (county roll)", 26), ("Who died", 22), ("Death evidence", 36), ("Died (about)", 12),
    ("Months since", 8), ("Probate", 14), ("Owners on title", 8), ("Decision makers on the record", 30),
    ("Value", 11), ("County appraisal", 11), ("Mortgage (est.)", 11), ("County tax behind", 12),
    ("County years behind", 12), ("City tax behind", 12), ("City years behind", 12), ("Turned over or tax suit", 10),
    ("Heirs it carries at $50K each", 10), ("Known heirs", 7), ("Each known heir clears $50K", 9), ("Liens", 30),
    ("Equity (est.)", 11), ("Free and clear", 7), ("Absentee", 7), ("Out of state mail", 7), ("Vacant", 7),
    ("Last county tax payment", 30), ("Foreclosure sale", 12), ("Record status", 14), ("Calls", 6), ("Texts", 6),
    ("Why it ranks here", 70), ("Flags", 48), ("Verify before an offer", 60), ("County assessor", 10),
    ("SiftMap id", 12), ("SmartSkip marks deceased", 20), ("Likely heirs (SmartSkip)", 40),
    ("Equity per likely heir", 11), ("Relatives found", 8),
]
WRAP_COLS = {"Why it ranks here", "Flags", "Verify before an offer", "Death evidence", "Liens", "Decision makers on the record",
             "Last county tax payment", "Owner of record (county roll)", "Likely heirs (SmartSkip)",
             "Heirs from the heir research", "Open questions (research)"}
MONEY_COLS = {"Value", "County appraisal", "Mortgage (est.)", "County tax behind", "City tax behind", "Equity (est.)",
              "Equity per likely heir", "Equity per heir (research)"}


def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    f = "Calibri"
    return {
        "title": Font(name=f, size=16, bold=True, color=NAVY), "sub": Font(name=f, size=10, color=GRAY),
        "h": Font(name=f, size=10, bold=True, color=NAVY), "b": Font(name=f, size=10, color="1F2937"),
        "bb": Font(name=f, size=10, bold=True, color="1F2937"), "link": Font(name=f, size=10, color=BLUE, underline="single"),
        "ok": Font(name=f, size=10, bold=True, color=GREEN), "cav": Font(name=f, size=10, color=GOLD),
        "head_fill": PatternFill("solid", fgColor="F3F5F9"), "cav_fill": PatternFill("solid", fgColor="FFF8E1"),
        "bot": Border(bottom=Side(style="thin", color=LINE)), "wrap": Alignment(wrap_text=True, vertical="top"),
        "top": Alignment(vertical="top"), "hdr": Alignment(wrap_text=True, vertical="bottom"),
    }


def _header(ws, row, cols, s, widths=None):
    from openpyxl.utils import get_column_letter
    for i, c in enumerate(cols, 1):
        cell = ws.cell(row=row, column=i, value=c)
        cell.font, cell.fill, cell.border, cell.alignment = s["h"], s["head_fill"], s["bot"], s["hdr"]
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[row].height = 30


def _title(ws, text, sub, s):
    ws["A1"] = text
    ws["A1"].font = s["title"]
    ws["A2"] = sub
    ws["A2"].font = s["sub"]


def _link(r) -> str:
    from datasift_links import record_url
    return record_url(r["uuids"][0]) if r.get("uuids") else ""


def _probate(r) -> str:
    p = ("open " + str(r.get("probate_month") or "")) if r.get("probate_open") else "none found"
    if r.get("probate_index_case") and (r.get("probate_src") == "index" or not r.get("probate_open") or r.get("story_verdict")):
        p = r["probate_index_case"]
    return p


def rank_row(i: int, r: dict) -> list:
    dec = C.nice(r.get("decedent_court") or r.get("dec_record") or r.get("decedent_name") or "")
    ev = "; ".join([("obituary index: " + c["row"] + ("" if c.get("verdict") == "confirmed" else " (same name appears more than once)"))
                    for c in r.get("index_checks") or [] if c.get("verdict") in ("confirmed", "likely") and c.get("row")]
                   or r.get("evidence") or [])
    payor = ""
    if r.get("last_payor"):
        payor = "%s, %s (paid %s bill)%s" % (C.nice(r["last_payor"]), C.fmt_day(r.get("last_pay_date")), r.get("last_paid_year") or "",
                                            " [family]" if r.get("payor_kind") == "family (same surname)"
                                            else (" [lender]" if r.get("payor_kind") == "lender or service" else ""))
    liens = "; ".join(r.get("lien_flags") or []) + ((" (%s on the record)" % C.dollars(r["crm_lien"])) if r.get("crm_lien") else "")
    return [i, r["score"], r["tier"], r["play"], r.get("story_verdict") or "", r.get("story_heirs") or "",
            r.get("story_per_heir"), r.get("story_reach") or "", r.get("story_open") or "",
            C.nice(r["street"]), C.nice(r.get("city")), r.get("zip"), r.get("county") or "", _link(r),
            r.get("roll_owner") or "; ".join(r.get("deed") or []), dec, ev, C.fmt_day(r.get("death_day2") or r.get("death_day")),
            r.get("months_since_death2") if r.get("months_since_death2") is not None else r.get("months_since_death"),
            _probate(r), r.get("n_owners"), "; ".join("%s (%s)" % (C.nice(n), str(rel or "").lower()) for n, rel in r.get("dms") or []),
            r.get("avm"), r.get("appraised"), r.get("mortgage") or 0, r.get("tax_amount") or 0, ", ".join(r.get("tax_years") or []),
            (r.get("city_amount") if r.get("city_read") else ("not read" if r.get("in_city") else "")),
            ", ".join(r.get("city_years") or []),
            "county" if r.get("turned_over") else ("city suit" if r.get("city_suit") else ""),
            r.get("carry"), r.get("known_heirs") or "", r.get("per_heir_ok") or "", liens.strip(), r.get("equity"),
            "yes" if r.get("free_clear") else "", "yes" if r.get("absentee") else "", r.get("mail_state") if r.get("oos") else "",
            "yes" if r.get("vacant") else "", payor, C.fmt_day(r.get("sale_day")), r.get("status") or "", r.get("calls") or 0,
            r.get("texts") or 0, r.get("why"), "; ".join(r.get("flags") or []), "; ".join(r.get("verify") or []),
            r.get("assessor_url") or "", str(r.get("dataflik_id") or ""),
            ", ".join(r.get("ss_dead") or []) or ("none flagged" if r.get("ss_run") else "not run"),
            (", ".join(r.get("ss_heirs") or []) + (" (" + r["ss_heir_order"] + ")" if r.get("ss_heir_order") else "")) if r.get("ss_run") else "",
            r.get("per_heir"), r.get("ss_relatives") or ""]


def write_ranked(ws, rows, s, start=4):
    from openpyxl.utils import get_column_letter
    names = [c for c, _ in RANK_COLS]
    _header(ws, start, names, s, [w for _, w in RANK_COLS])
    for i, r in enumerate(rows, 1):
        rr = start + i
        for j, v in enumerate(rank_row(i, r), 1):
            cell = ws.cell(row=rr, column=j, value=v)
            name = names[j - 1]
            cell.font = s["b"]
            cell.alignment = s["wrap"] if name in WRAP_COLS else s["top"]
            if name in MONEY_COLS and isinstance(v, (int, float)):
                cell.number_format = MONEY
            if name == "DataSift record" and v:
                cell.value, cell.hyperlink, cell.font = "Open record", v, s["link"]
            if name == "County assessor" and v:
                cell.value, cell.hyperlink, cell.font = "Assessor", v, s["link"]
            if name == "Tier" and str(v).startswith("1"):
                cell.font = s["ok"]
            if name == "Flags" and v:
                cell.font = s["cav"]
        ws.row_dimensions[rr].height = 75
    ws.freeze_panes = ws.cell(row=start + 1, column=6)
    ws.auto_filter.ref = "A%d:%s%d" % (start, get_column_letter(len(RANK_COLS)), start + len(rows))


def sources(rows: list[dict], ws_) -> list[tuple[str, str]]:
    fips = Counter(r.get("fips") or "" for r in rows)
    out = [("Your records", "%d houses from the lists and tags in scope, read over your DataSift Open API key: lists, tags, "
                            "custom fields (decedent, decision makers, sale date), status, attempts." % len(rows)),
           ("SiftMap detail", "Deed owners and co-owners, the county appraisal, death, estate and lien flags, sale and loan "
                              "history, for houses already in your account.")]
    for f, n in fips.most_common():
        cty = county_for(fips=f)
        srcs = cty.sources()
        out.append((cty.label if f else "Unknown county", "%d houses. %s" % (
            n, ("Read against " + "; ".join(srcs) + ".") if srcs else "No county source is built in for this county yet: "
            "ranked on the record and SiftMap alone (no live tax roll, no obituary or probate index).")))
    fam = ws_.read("family.json", {}) or {}
    if fam:
        out.append(("SmartSkip", "%d houses with a family tree (%d relatives). A deceased flag on a title holder counts as "
                                 "the death; relationships are SmartSkip's coarse guess." % (len(fam), sum(len(v.get("relatives") or []) for v in fam.values()))))
    out.append(("Heir research", "Where a house has an heir story, each heir was tied by a record that names the parent "
                                 "(obituaries, Find a Grave, Social Security, birth and marriage indexes), with stepchildren, "
                                 "in-laws and same-name strangers listed as not heirs. A title company and your attorney "
                                 "confirm the heirs before any closing."))
    return out


def build(ws_, out=print) -> str:
    from openpyxl import Workbook
    s = _styles()
    rows = ws_.read("scored.json", []) or []
    today = C.today()
    kept = [r for r in rows if r["tier"] != "Out"]
    kept.sort(key=lambda r: (r["tier"], r.get("story_first", 1), -r["score"], -(r.get("equity") or 0)))
    ranked = [r for r in kept if not str(r["play"]).startswith(("Probate open: a probate deal", "No death signal"))]
    probate = [r for r in kept if str(r["play"]).startswith("Probate open: a probate deal")]
    nodeath = [r for r in kept if str(r["play"]).startswith("No death signal")]
    out_rows = [r for r in rows if r["tier"] == "Out"]
    taxes = sorted([r for r in rows if (r.get("tax_total") or 0) > 0], key=lambda r: (r["tier"] == "Out", -(r.get("tax_total") or 0)))
    work = [r for r in ranked if r["tier"] in ("1 Work now", "2 Research first")]
    tiers = Counter(r["tier"] for r in kept)
    reasons = Counter(("listed on the MLS" if r["disq"].startswith("listed") else
                       "sold after the death" if r["disq"].startswith("sold after") else r["disq"]) for r in out_rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    label = ws_.market.replace("-", " ").title()
    _title(ws, "%s curative title opportunities" % label,
           "Ranked %s. Internal: carries owner and family names." % today.strftime("%b %d, %Y").replace(" 0", " "), s)
    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 14
    for c in "CDEFG":
        ws.column_dimensions[c].width = 16
    lines = [
        "Every house we can tie to an owner's death, ranked on its odds of being a curative title deal: the owner of record "
        "died, the title is still stuck in that name, nobody opened a probate, the taxes or liens are behind, and there "
        "is enough equity to pay each heir their share.",
        "Tier 1 is ready to work: research the heirs, then call them. Tier 2 needs one fact settled first (usually the "
        "death itself or a second owner). Probate deals are listed apart: an open estate is a probate lead, not a "
        "curative one.",
        "Houses with an heir story show the heirs found, each share, the equity per heir and how many heirs have a "
        "confirmed number. Their tier comes from that research, and the ones ready to work sort first.",
    ]
    r0 = 4
    for i, t in enumerate(lines):
        c = ws.cell(row=r0 + i, column=1, value=t)
        c.font, c.alignment = s["b"], s["wrap"]
        ws.merge_cells(start_row=r0 + i, start_column=1, end_row=r0 + i, end_column=7)
        ws.row_dimensions[r0 + i].height = 42
    r = r0 + len(lines) + 1
    ws.cell(row=r, column=1, value="The count").font = s["bb"]
    funnel = [("Houses read", len(rows))] + [("Ruled out: %s" % k, v) for k, v in reasons.most_common()] + [
        ("Kept", len(kept)), ("  Tier 1: work now", tiers.get("1 Work now", 0)),
        ("  Tier 2: research first", tiers.get("2 Research first", 0)), ("  Tier 3: watch", tiers.get("3 Watch", 0)),
        ("Probate deals", len(probate)), ("Behind on county or city tax", len(taxes))]
    _header(ws, r + 1, ["Step", "Houses"], s)
    for i, (k, v) in enumerate(funnel):
        ws.cell(row=r + 2 + i, column=1, value=k).font = s["b"]
        c = ws.cell(row=r + 2 + i, column=2, value=v)
        c.font, c.number_format = s["b"], "#,##0"
    r = r + 3 + len(funnel)
    ws.cell(row=r, column=1, value="The first ten").font = s["bb"]
    _header(ws, r + 1, ["Property", "Score", "Play", "Tax behind", "Equity", "Who died", "Why"], s)
    ws.column_dimensions["G"].width = 90
    for i, x in enumerate(ranked[:10]):
        rr = r + 2 + i
        vals = [C.nice(x["street"]), x["score"], x["play"], x.get("tax_total") or 0, x.get("equity"),
                C.nice(x.get("decedent_court") or x.get("dec_record") or x.get("decedent_name")), x["why"]]
        for j, v in enumerate(vals, 1):
            c = ws.cell(row=rr, column=j, value=v)
            c.font, c.alignment = s["b"], s["wrap"]
            if j in (4, 5) and isinstance(v, (int, float)):
                c.number_format = MONEY
        ws.row_dimensions[rr].height = 60
    r = r + 3 + 10
    ws.cell(row=r, column=1, value="Read these before acting").font = s["bb"]
    for i, t in enumerate(CAVEATS):
        c = ws.cell(row=r + 1 + i, column=1, value=t)
        c.font, c.fill, c.alignment = s["cav"], s["cav_fill"], s["wrap"]
        ws.merge_cells(start_row=r + 1 + i, start_column=1, end_row=r + 1 + i, end_column=7)
        ws.row_dimensions[r + 1 + i].height = 32

    for title, sub, data in (
            ("Work list", "Tiers 1 and 2, best first: %d houses. Tier 1 is ready for the heir research and the calls; tier 2 "
                          "needs one fact settled first (see Flags and Verify)." % len(work), work),
            ("All ranked", "%d houses. Tier 1 first, then by score." % len(ranked), ranked),
            ("Taxes behind", "%d houses owe delinquent county or city tax on the offices' own bills. A current year's bill "
                             "is never counted as late." % len(taxes), taxes),
            ("Probate deals", "%d houses where an estate is already open: the family is organized, so these are probate "
                              "leads rather than curative ones." % len(probate), probate),
            ("Foreclosures, no death", "%d houses with no sign the owner died: a living owner can sell normally." % len(nodeath), nodeath)):
        sh = wb.create_sheet(title)
        _title(sh, title, sub, s)
        write_ranked(sh, data, s)

    family = ws_.read("family.json", {}) or {}
    if family:
        from openpyxl.utils import get_column_letter
        wsf = wb.create_sheet("Family trees", 2)
        order = [r for r in work if r["key"] in family] + [r for r in ranked + probate if r["key"] in family and r not in work]
        _title(wsf, "Family trees (SmartSkip)", "%d houses, one row per relative. Relationships are SmartSkip's own guess; "
               "the obituary settles who is a child. The likely heir follows the usual no-will order (spouse and children, "
               "else parents, else siblings): a research aid, not a legal answer." % len(order), s)
        cols = [("Rank", 6), ("Tier", 13), ("Property", 26), ("Who died", 24), ("SmartSkip marks deceased", 22),
                ("Relative", 24), ("Relationship (SmartSkip)", 14), ("Age", 6), ("Likely heir", 8), ("Lives in", 22),
                ("Phones", 9), ("Traced from", 12), ("DataSift record", 12)]
        _header(wsf, 4, [c for c, _ in cols], s, [w for _, w in cols])
        rank_of = {r["key"]: i for i, r in enumerate(work, 1)}
        rr = 4
        for r in order:
            f = family[r["key"]]
            heirs = set(f.get("heirs") or [])
            dead = ", ".join(x["name"] for x in f.get("subjects") or [] if x.get("deceased")) or "none flagged"
            for x in f.get("relatives") or [{"name": "(no relatives returned)", "rel": "", "phones": []}]:
                rr += 1
                vals = [rank_of.get(r["key"], ""), r["tier"], C.nice(r["street"]),
                        C.nice(r.get("decedent_court") or r.get("dec_record") or r.get("decedent_name") or ""), dead,
                        x["name"], x.get("rel"), x.get("age") or "", "yes" if x["name"] in heirs else "",
                        ", ".join(v for v in (x.get("city"), x.get("state")) if v), len(x.get("phones") or []),
                        x.get("of") or "", _link(r)]
                for j, v in enumerate(vals, 1):
                    c = wsf.cell(row=rr, column=j, value=v)
                    c.font, c.alignment = s["b"], s["top"]
                    if j == 13 and v:
                        c.value, c.hyperlink, c.font = "Open record", v, s["link"]
                    if j == 9 and v:
                        c.font = s["ok"]
        wsf.freeze_panes = "D5"
        wsf.auto_filter.ref = "A4:%s%d" % (get_column_letter(len(cols)), rr)

    w7 = wb.create_sheet("Ruled out")
    _title(w7, "Ruled out", "%d houses, each with the reason." % len(out_rows), s)
    _header(w7, 4, ["Property", "City", "ZIP", "DataSift record", "Reason", "Play", "Value", "Equity (est.)", "Owner of record"], s,
            [28, 12, 7, 12, 44, 26, 11, 11, 30])
    for i, x in enumerate(sorted(out_rows, key=lambda x: (x["disq"], x["street"])), 1):
        vals = [C.nice(x["street"]), C.nice(x.get("city")), x.get("zip"), _link(x), x["disq"], x["play"], x.get("avm"),
                x.get("equity"), x.get("roll_owner") or "; ".join(x.get("deed") or [])]
        for j, v in enumerate(vals, 1):
            c = w7.cell(row=4 + i, column=j, value=v)
            c.font = s["b"]
            if j == 4 and v:
                c.value, c.hyperlink, c.font = "Open record", v, s["link"]
            if j in (7, 8) and isinstance(v, (int, float)):
                c.number_format = MONEY
    w7.freeze_panes = "B5"
    w7.auto_filter.ref = "A4:I%d" % (4 + len(out_rows))

    w8 = wb.create_sheet("Sources and method")
    _title(w8, "Sources and method", "What was read, and how each house is scored.", s)
    w8.column_dimensions["A"].width = 34
    w8.column_dimensions["B"].width = 110
    rr = 4
    for k, v in sources(rows, ws_):
        w8.cell(row=rr, column=1, value=k).font = s["bb"]
        c = w8.cell(row=rr, column=2, value=v)
        c.font, c.alignment = s["b"], s["wrap"]
        w8.row_dimensions[rr].height = 32
        rr += 1
    rr += 1
    w8.cell(row=rr, column=1, value="The score (105 points, shown out of 100)").font = s["bb"]
    rr += 1
    for k, v in WEIGHTS:
        w8.cell(row=rr, column=1, value=k).font = s["b"]
        c = w8.cell(row=rr, column=2, value=v)
        c.font, c.alignment = s["b"], s["wrap"]
        w8.row_dimensions[rr].height = 30
        rr += 1
    rr += 1
    w8.cell(row=rr, column=1, value="Guardrails").font = s["bb"]
    rr += 1
    for v in GUARDRAILS:
        c = w8.cell(row=rr, column=1, value=v)
        c.font, c.fill, c.alignment = s["cav"], s["cav_fill"], s["wrap"]
        w8.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=2)
        w8.row_dimensions[rr].height = 30
        rr += 1

    path = ws_.root / ("%s_Curative_Title_%s.xlsx" % (label.replace(" ", "_"), today.isoformat()))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + "_PENDING_.xlsx")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except PermissionError:          # the workbook is open in Excel: keep it, write the next version beside it
        n = 2
        while True:
            alt = path.with_name("%s_v%d.xlsx" % (path.stem, n))
            try:
                os.replace(tmp, alt)
                path = alt
                break
            except PermissionError:
                n += 1
    out("wrote %s (work list %d, ranked %d, probate %d, ruled out %d)" % (path, len(work), len(ranked), len(probate), len(out_rows)))
    return str(path)
