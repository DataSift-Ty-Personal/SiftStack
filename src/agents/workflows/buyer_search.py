"""buyer_search: the buyers most likely to buy one house, on this install's own DataSift key.

SiftStack's own version of the data team's buyer model (src/buyer_search/open_api.py). The model itself
reads the production property cluster, which no user can reach, so this runs the same pipeline on what an
install's Open API key can read, and scores with the model's exact math:

  * the house: its DataSift record (a record uuid or its street line), or any street line with its city,
    state and ZIP (the Census geocoder places it and a SiftMap map search finds the parcel);
  * every investor purchase within the radius from SiftMap's documented search, the model's similar-house
    bands applied before any deed is read, then the nearest houses like this one read for who bought them;
  * each buyer's ten score parts rebuilt from those deeds and the houses they hold, scored 0 to 100.

Deed reads share the deal workflows' daily budget (AGENTS_DEAL_READS_PER_DAY) and stop at
AGENTS_BUYER_SEARCH_READS an ask (150; an ask may say `reads`, at most 300): Acceptable use forbids SiftMap
detail pulls in volume without adding the records. Builders, iBuyers, funds, lenders and government bodies are
marked and set aside, never dropped.

What each would pay, a guess: every buyer carries `likely_price` (open_api.price_guesses): each of their deeds in
this search priced for this house on its own terms (a flip's buy over its resale times this house's ARV, the `arv`
the ask gives or else its estimated value; any other purchase's price over the house's value times this house's
estimated value), with the area's typical counted as one more deed so one odd deal cannot swing it. Each Slack
line names the deal it rests on; one line gives the area's typical. With today's deed reads spent, a house
already searched answers from the deeds kept for it.

Past the top 10: every buyer the deeds found is ranked (up to 250) and in the workbook; Slack lists `limit` of
them (10, at most 50) starting at `start`, so "buyers 11 to 30" is start 11, limit 20. A house's deeds are kept
for AGENTS_BUYER_SEARCH_CACHE_DAYS (14), so asking again for the same house reads only what the last ask left
unread and the list grows without reading anything twice.

It reads and nothing else: no DataSift write, no vendor, no cost. deal_buyers is the deeper one for a
contracted house (decision makers and how many numbers are on file); this is the quick, wide answer to
"who buys this house".
"""
from __future__ import annotations

import csv
import re

from .. import config
from .quick_screen import (MissingKey, count_reads, deed_reads_left, file_line, files_dir, inputs_of, locate,
                           safe_text, setting, siftmap, subject_of)

NAME = "buyer_search"
SHOW = 10           # buyers listed in Slack unless the ask says otherwise
MAX_SHOW = 50       # the most one Slack answer lists; every buyer is in the workbook
ALL_BUYERS = 250    # the ranked list is never cut short of the buyers the deeds found
MAX_READS = 300     # one ask's deed reads, whatever it asks for (the day's budget still applies)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
COLUMNS = ["target_rank", "score", "likely_price", "price_share", "priced_purchases", "median_paid", "not_a_target",
           "buyer_full_name", "buyer_address", "buyer_city", "buyer_state",
           "buyer_zip", "is_buyer_corporation", "purchases_radius", "portfolio", "total_investor_purchases",
           "purchases_per_year", "locality_percentage", "median_discount", "month_active_perc", "active_years",
           "similarity", "buyer_size", "profile_size", "last_purchase", "cash_share", "buyer_id"]
HEADERS = {
    "target_rank": "Call order", "score": "Score (0-100)", "likely_price": "Likely to pay for this house (a guess)",
    "price_share": "Their deeds: median price over worth (%)", "priced_purchases": "Deeds with a usable price",
    "median_paid": "Median paid for houses like this here", "not_a_target": "Set aside because",
    "buyer_full_name": "Buyer", "buyer_address": "Mailing address", "buyer_city": "City", "buyer_state": "State",
    "buyer_zip": "ZIP", "is_buyer_corporation": "Company", "purchases_radius": "Bought a house like this here",
    "portfolio": "Houses held (read off a deed; 11 means 11 or more; empty when not read)",
    "total_investor_purchases": "Purchases counted (score part)", "purchases_per_year": "Purchases a year here",
    "locality_percentage": "Share bought here (%)", "median_discount": "Median discount (%)",
    "month_active_perc": "Months active (%)", "active_years": "Years active", "similarity": "Profile match",
    "buyer_size": "Buyer size", "profile_size": "Houses in profile", "last_purchase": "Last purchase here",
    "cash_share": "Cash share", "buyer_id": "Buyer key",
}


# What each listed buyer carries into the Slack line (render reads nothing else; the selftest checks every field
# the line uses is here, since a missing one shows as a generic phrase rather than an error).
TOP_KEYS = ("target_rank", "buyer_full_name", "buyer_city", "buyer_state", "score", "purchases_radius",
            "total_investor_purchases", "portfolio", "last_purchase", "similarity", "likely_price", "likely_share",
            "price_share", "priced_purchases", "priced_deeds", "unpriced")


def _yes(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _num(v):
    s = re.sub(r"[^\d.]", "", str(v or ""))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def subject(task: dict, inputs: dict, sm) -> tuple[dict | None, str]:
    """The house: a DataSift record when the request names one, else any street line placed on the map."""
    from buyer_search import open_api
    ref = str(inputs.get("address") or inputs.get("record") or "").strip()
    if not ref and not task.get("record_uuid"):
        return None, "name the house: a record uuid, or its street line with city, state and ZIP"
    rec, _, why, _ = locate(task, inputs)        # a pasted "street, city, state ZIP" finds its record
    if rec:
        s = open_api.subject_from_record(subject_of(rec))
        if s.get("lat") and s.get("lon"):
            return lot_from_map(sm, s), ""
        ref = ", ".join(x for x in (s["address"], s["city"], s["state"], s["zip"]) if x)
    if task.get("record_uuid") and not rec or _UUID.match(ref) or not re.match(r"\s*\d+\s+\S", ref):
        return None, why or "the house was not found"
    return open_api.subject_from_siftmap(sm, ref)


def lot_from_map(sm, s: dict) -> dict:
    """A record carries no lot size, so its own SiftMap row (one map search around its point, matched on the
    dataflik_id) supplies it: a record and a typed address then apply the same lot band. Unchanged when the
    row is not found."""
    if s.get("lot_sf") or not s.get("dataflik_id"):
        return s
    from buyer_search import open_api
    try:
        rows = sm.search({"polygon": open_api._dbm().box(float(s["lat"]), float(s["lon"]), 0.1), "size": 100,
                          "result_index": 0, "with_boundaries": False}).get("data") or []
    except Exception:  # noqa: BLE001  (no lot band then, as before)
        return s
    row = next((r for r in rows if str(r.get("id") or "") == str(s["dataflik_id"])), None)
    acres = open_api._num((row or {}).get("lotAcres"))
    return dict(s, lot_sf=round(acres * open_api.SQFT_PER_ACRE)) if acres else s


def house_folder(subj: dict) -> "Path":
    """Where one house's deeds are kept between asks (AGENTS_BUYER_SEARCH_CACHE_DAYS, 14), so asking again reads
    only what the last ask left unread: that is how a list grows past one ask's read cap."""
    import shutil
    from datetime import date
    from pathlib import Path
    key = re.sub(r"[^A-Za-z0-9]+", "-", str(subj.get("dataflik_id") or subj.get("address") or "house")).strip("-")
    d = Path(config.RUNS_DIR) / "buyer_search_houses" / (key[:60] or "house")
    made = d / "made.txt"
    if made.is_file():
        try:
            age = (date.today() - date.fromisoformat(made.read_text(encoding="utf-8").strip())).days
        except ValueError:
            age = 10 ** 6
        if age > int(setting("AGENTS_BUYER_SEARCH_CACHE_DAYS", 14)):
            shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    if not made.is_file():
        made.write_text(date.today().isoformat(), encoding="utf-8")
    return d


def write_files(folder, result: dict) -> list[str]:
    rows = result.get("buyers") or []
    with open(folder / "buyers.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writerow({c: HEADERS[c] for c in COLUMNS})
        for b in rows:
            w.writerow(b)
    names = ["buyers.csv"]
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Border, Font, Side
        wb = Workbook()
        ws = wb.active
        ws.title = "Buyers"
        ws.append([HEADERS[c] for c in COLUMNS] + ["Bought here (newest first)", "The deals the guess rests on"])
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.border = Border(bottom=Side(style="thin", color="9AA3B2"))
        for b in rows:
            here = "; ".join("%s %s%s" % (x.get("address") or "", x.get("date") or "",
                                          (" $%s" % format(int(x["price"]), ",")) if x.get("price") else "")
                             for x in b.get("bought_here") or [])
            deals = "; ".join("%s %s: %s" % (d.get("address") or "", d.get("date") or "", deal_text(d))
                              for d in b.get("priced_deeds") or [])
            ws.append([b.get(c) for c in COLUMNS] + [here, deals])
        ws.freeze_panes = "A2"
        for i, c in enumerate(COLUMNS + ["here", "deals"], start=1):
            head = HEADERS.get(c, "Bought here (newest first)")
            width = max([len(head)] + [len(str(b.get(c) or "")) for b in rows[:200]]) if c in HEADERS else 60
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max(8, width + 2), 60)
        about = wb.create_sheet("How it was scored")
        subj, c, p = result.get("subject") or {}, result.get("counts") or {}, result.get("params") or {}
        about.append(["House", subj.get("address")])
        about.append(["Searched", "%s miles, %s months; %s investor sales seen, %s like this house; %s deeds read, "
                                  "%s left unread at the cap" % (p.get("radius_miles"), p.get("months"),
                                                                 c.get("investor_sales_seen"), c.get("similar"),
                                                                 c.get("deeds_read"), c.get("deeds_unread"))])
        about.append(["Model", "The data team's buyer search (weights, scales and fills unchanged), rebuilt on "
                               "SiftMap reads this account's key can make"])
        about.append(["Score", "Ten parts, each scaled 0 to 1 and weighted: investor purchases (0.25), profile "
                               "match (0.25), purchases a year, share bought here, buyer size (0.10 each), median "
                               "discount, months active, years active, houses in profile, purchases here (0.05 each)"])
        for k, v in (result.get("components") or {}).items():
            about.append([HEADERS.get(k, k), v])
        pr = result.get("pricing") or {}
        if pr.get("how"):
            about.append([HEADERS["likely_price"], pr["how"]])
            about.append(["Priced against", _pricing_line(pr)])
        about.append(["Set aside", "Your own company, builders, iBuyers and funds, lenders and servicers, churches, "
                                   "government bodies, land banks and nonprofits. Kept in the list with the reason."])
        about.column_dimensions["A"].width = 30
        about.column_dimensions["B"].width = 120
        wb.save(folder / "Buyers.xlsx")
        names.insert(0, "Buyers.xlsx")
    except Exception:  # noqa: BLE001  (the CSV still carries everything)
        pass
    return names


def run(task: dict) -> dict:
    from buyer_search import client
    from .deal_buyers import CountingMap
    inputs, _ = inputs_of(task)
    out = {"ok": False, "files": [], "live": config.live()}
    asked = _num(inputs.get("reads"))
    per_ask = int(asked) if asked else int(setting("AGENTS_BUYER_SEARCH_READS", 150))
    cap = min(per_ask, deed_reads_left(), MAX_READS)
    try:
        sm = CountingMap(siftmap())
    except MissingKey as exc:
        out["why"] = str(exc)
        return out
    try:
        subj, why = subject(task, inputs, sm)
    except Exception as exc:  # noqa: BLE001  (SiftMap or the geocoder refusing: said, not raised)
        subj, why = None, "the house could not be found: %s" % str(exc)[:160]
    if not subj:
        count_reads(sm.details)
        out["why"] = why
        return out
    work = house_folder(subj)
    # With no reads left, a house already searched still answers from the deeds kept for it (an ARV re-ask, say).
    if cap <= 0 and not (work / "details.json").is_file():
        count_reads(sm.details)
        out["why"] = ("today's SiftMap deed reads are used (AGENTS_DEAL_READS_PER_DAY) or the run cap is 0, so no "
                      "buyer could be read; ask again tomorrow")
        return out
    out["budget_spent"] = cap <= 0
    cap = max(cap, 0)
    out["street"] = subj.get("address") or ""
    radius = _num(inputs.get("radius"))
    months = _num(inputs.get("months"))
    folder = files_dir(task, NAME)
    try:
        result = client.search_open(subject=subj, client=sm, work=work, radius_mi=radius or None,
                                    months=int(months) if months else None, read_cap=cap,
                                    params={"limit": ALL_BUYERS}, arv=_num(inputs.get("arv")))
    except client.SearchFailed as exc:
        count_reads(sm.details)
        out["why"] = str(exc)
        return out
    count_reads(sm.details)
    if result.get("status") != 200:
        out["why"] = result.get("error") or "the search failed"
        return out
    out["files"] = write_files(folder, result)
    want = max(1, min(int(_num(inputs.get("limit")) or SHOW), MAX_SHOW))
    start = max(1, int(_num(inputs.get("start")) or 1))
    targets = [b for b in result.get("buyers") or [] if b.get("target_rank")]
    reasons: dict[str, int] = {}
    for b in result.get("buyers") or []:
        if b.get("not_a_target"):
            kind = b["not_a_target"].split(" (")[0]
            reasons[kind] = reasons.get(kind, 0) + 1
    out.update(ok=True, subject=result.get("subject") or {}, counts=result.get("counts") or {},
               params=result.get("params") or {}, reads=sm.details, searches=sm.searches, marked=reasons,
               start=start, shown_of=len(targets), cap=cap, pricing=result.get("pricing") or {},
               top=[{k: b.get(k) for k in TOP_KEYS} for b in targets[start - 1:start - 1 + want]])
    return out


def _n(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return format(v, ",") if isinstance(v, int) else str(v)


def _money(v) -> str:
    return "$%s" % format(int(round(float(v))), ",") if v else "no value"


DEAL_WORDS = {"resale": "bought for %s, resold for %s", "prior sale": "paid %s for a house that sold for %s before",
              "value": "paid %s for a house now valued at %s"}


def deal_text(d: dict) -> str:
    """One priced deed in words: what they paid and what the house was worth."""
    return DEAL_WORDS.get(d.get("basis"), DEAL_WORDS["value"]) % (_money(d.get("price")), _money(d.get("worth")))


def _guess(b: dict) -> str:
    """One buyer's price guess for the Slack line, with the deal it rests on."""
    if not b.get("likely_price"):
        return "no price guess"
    deeds = b.get("priced_deeds") or []
    if len(deeds) == 1:
        ev = deal_text(deeds[0])
    elif deeds:
        ev = "paid %s%% of worth over %d deeds" % (_n(round(float(b.get("price_share") or 0))), len(deeds))
    else:
        ev = "%s, so the area's typical" % (b.get("unpriced") or "no usable price on their deeds")
    return "would pay about %s (%s)" % (_money(b["likely_price"]), ev)


def _pricing_line(pr: dict) -> str:
    """What the buyers to call would pay, and what each kind of deal was priced against."""
    if not pr:
        return ""
    if not (pr.get("value") or pr.get("arv")):
        return "No price guesses: the house has no estimated value. Ask again with an ARV to price against it."
    if not pr.get("usual_price"):
        return "No price guesses: none of the buyers to call has a priced deed here."
    value, arv = pr.get("value"), pr.get("arv")
    parts = []
    if pr.get("holder_share") is not None:
        parts.append("holders paid a median %s%% of a house's value as it stood, applied to %s" % (
            _n(round(float(pr["holder_share"]))),
            ("this house's %s estimate" % _money(value)) if value else ("the ARV, %s, since it has no estimate"
                                                                         % _money(arv))))
    if pr.get("flip_share") is not None:
        parts.append("flippers bought at a median %s%% of their resale, applied to %s" % (
            _n(round(float(pr["flip_share"]))),
            ("your ARV, %s" % _money(arv)) if arv else ("the same %s, since no ARV was given" % _money(value))))
    spread = (" Buyer by buyer, %s to %s." % (_money(pr["low"]), _money(pr["high"]))
              if pr.get("low") and pr.get("high") and pr["low"] != pr["high"] else "")
    ask = (" Ask with an ARV so the flippers' deals price against it." if not arv and pr.get("flip_share") is not None
           else "")
    return ("What they would pay, a guess: about %s here, the median of %s deals by the buyers to call, each priced "
            "for this house (%s). Each buyer's guess counts that median as one more deal beside their own.%s%s" % (
                _money(pr["usual_price"]), _n(pr.get("priced_purchases")), "; ".join(parts), spread, ask))


def render(task: dict, r: dict) -> str:
    s = r.get("subject") or {}
    head = ":mag: *Buyers for %s*" % (s.get("address") or r.get("street") or task.get("subject") or "this house")
    if not r.get("ok"):
        return safe_text(head + "\nNot run: %s" % (r.get("why") or "no reason given"))
    c, p = r.get("counts") or {}, r.get("params") or {}
    lines = [head + "  (the data team's buyer model on this account's SiftMap, %s miles, %s months)" % (
        _n(p.get("radius_miles")), _n(p.get("months")))]
    span = (" between %s and %s" % (c.get("sale_date_first"), c.get("sale_date_last"))
            if c.get("sale_date_first") else "")
    if not c.get("buyers"):
        lines.append("No investor bought a house like this one nearby in the deeds read (%s investor sales seen, "
                     "%s like it)." % (_n(c.get("investor_sales_seen")), _n(c.get("similar"))))
    else:
        aside = ", ".join("%d %s" % (n, k) for k, n in sorted((r.get("marked") or {}).items()))
        lines.append("%s buyers bought a house like this nearby%s; %s are buyers we would call%s." % (
            _n(c.get("buyers")), span, _n(c.get("targets")), (" (%s set aside)" % aside) if aside else ""))
        if _pricing_line(r.get("pricing") or {}):
            lines.append(_pricing_line(r.get("pricing") or {}))
    total, start, top = int(r.get("shown_of") or 0), int(r.get("start") or 1), r.get("top") or []
    end = start + len(top) - 1
    if top:
        lines.append(("Top %d of %s to call:" % (len(top), _n(total))) if start == 1 else
                     ("Buyers %d to %d of %s to call:" % (start, end, _n(total))))
    elif total and start > total:
        lines.append("There are %s buyers to call; ask with a start of %s or lower." % (_n(total), _n(total)))
    for b in top:
        held = b.get("portfolio")        # None: no house of theirs was read (a flip's seller, say), so unknown
        holds = (("holds 11 or more" if held >= 11 else "holds %s" % _n(held)) if held is not None
                 else "holdings not read")
        lines.append("%s. %s (%s): score %s, %s; bought %s here, %s, last bought %s" % (
            b.get("target_rank"), b.get("buyer_full_name"),
            ", ".join(x for x in (b.get("buyer_city"), b.get("buyer_state")) if x) or "mailing address not read",
            _n(b.get("score")), _guess(b), _n(b.get("purchases_radius")), holds, b.get("last_purchase") or "-"))
    if top and end < total:
        lines.append("%d more to call: ask for start %d (up to %d at a time), or open the workbook for all %s." % (
            total - end, end + 1, MAX_SHOW, _n(total)))
    unread = int(c.get("deeds_unread") or 0)
    reads = "Deed reads this ask: %s" % _n(r.get("reads"))
    if r.get("budget_spent"):
        reads += (" (today's deed reads are used, so this answers from the %s deeds already read for this house%s)"
                  % (_n(c.get("deeds_read")), ("; %s more nearby houses like this one can be read on an ask tomorrow"
                                               % _n(unread)) if unread else ""))
    elif unread:
        reads += ("; %s nearby houses like this one are still unread at this ask's cap of %s. Ask again for the "
                  "same house and only those are read, so the list grows" % (_n(unread), _n(p.get("read_cap"))))
    lines.append(reads + ".")
    lines.append("How it works: SiftMap's investor purchases within %s miles over %s months, kept to houses like this "
                 "one (value, beds, size, lot, age); their deeds read nearest first to see who bought; every buyer "
                 "scored 0 to 100 on the data team's ten parts (track record, how close what they buy is to this "
                 "house, how local and how active). Each price is a guess: each of that buyer's deals priced for "
                 "this house (a flip's buy over its resale times this house's ARV, any other purchase's price over "
                 "the house's value times this house's value), blended with the area's typical. The workbook's "
                 "second sheet explains each part." % (
                     _n(p.get("radius_miles")), _n(p.get("months"))))
    for f in r.get("files") or []:
        lines.append(file_line(task, "Every buyer with each score part" if f.endswith(".xlsx") else "Same as CSV", f))
    lines.append("Read only: nothing written to DataSift, nobody contacted. For decision makers and their "
                 "numbers, ask for deal_buyers on the same house.")
    return safe_text("\n".join(lines))
