"""underwrite: the post-walkthrough package for one house, built on the brain for Roma.

What src/post_walkthrough.py plus src/offer_sheet.py build on the workstation, in three shapes
decided by what the request carries:

  * REHAB ONLY: a walk and no comps asked for (no boundary, no "radius", no streets). Ty drops the
    walkthrough photos in Slack, Roma writes the walk JSON from them (photo_notes: one entry per photo
    or room) and this builds <street>_Rehab.xlsx: the photo findings, ONE rehab number, the repair
    detail, and comp and offer tabs that say in a sentence that no comps were asked for.
  * FULL: a walk and comps asked for. The saved pack (<street>_pack.json, what offer_sheet re-renders
    from: hand it "task 41"), the walk used, the post-walkthrough workbook and the one-tab offer sheet.
  * PRE-WALK: no walk. The pack and the workbook, and no offer: without a walk the rehab is sight
    unseen and an offer off it is the number Ty said never to quote (2026-08-27).

The rehab is always ONE plan: the walk's single_scenario, else the engine's own mid plan under the
same key, said out loud; never the four-scenario menu. A credit or flag the plan leaves out is named.

What beats what: the operator's notes (beds, baths, sqft, year built) beat the record and Zillow, as
the --beds/--baths/--sqft/--year-built flags do, and a layout in the notes holds the walk's target and
plan (an operator note beats what a photo suggests). The house is held to the layout it has unless
the walk says a reconfiguration was verified. A stated repair in the notes is carried into the
workbook as operator notes; the walk JSON is where it changes a number.

The house can be a DataSift record (uuid or street line) or, for rehab only, an address that is not
one yet when the notes give its square footage. Comps are the quick screen's: SiftMap deeds first
(free, capped), the Zillow pull when asked in Slack (paid, AGENTS_COMPS_DAILY_USD). Nothing is written
to DataSift.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import store
from .quick_screen import (Budget, MissingKey, apply_notes, area_inputs, area_line, arv_of, build_pack, comps_asked,
                           comps_for, file_line, files_dir, inputs_of, json_input, lead_for, locate, materials_line,
                           notes_beat_walk, pinned_walk, rehab_pick, safe_text, setting, subject_of, usd, zillow_line)

NAME = "underwrite"


def resolve_subject(task: dict, inputs: dict) -> tuple[dict | None, dict | None, dict, str]:
    """(subject, record or None, custom fields, why). A record uuid or a street line that matches one
    record is read; a street line that matches none is a house not in DataSift yet (rehab only);
    two matches are refused rather than guessed between."""
    import comp_package
    rec, fields, why, missing = locate(task, inputs)
    if rec:
        return subject_of(rec), rec, fields, ""
    if missing:
        ref = str(inputs.get("address") or inputs.get("record") or "")
        street, city, state, zip5 = comp_package.split_address(ref)
        return subject_of({"address": {"street": street, "city": city, "state": state, "postal_code": zip5}}), None, {}, ""
    return None, None, {}, why


def _pw_subject(subj: dict) -> dict:
    where = ", ".join(x for x in (subj["street"], subj["city"], ("%s %s" % (subj["state"], subj["zip"])).strip()) if x)
    return {"address": subj["street"], "short_address": subj["street"], "full_address": where,
            "beds": subj["beds"] or 3, "baths": subj["baths"] or 1.0, "sqft": subj["sqft"],
            "year_built": subj["year_built"], "county": subj["county"]}


def rehab_only(task: dict, subj: dict, walk: dict, folder: Path, stem: str, notes_lines: list, context: str,
               out: dict) -> dict:
    import post_walkthrough as pw
    book = stem + "_Rehab.xlsx"
    res = pw.build_rehab_workbook(_pw_subject(subj), walk, str(folder / book), materials=materials_line(),
                                  operator_notes=context, notes=notes_lines)
    walk_name = stem + "_walk.json"
    (folder / walk_name).write_text(json.dumps(res["walk"], indent=1, default=str), encoding="utf-8")
    out.update(ok=True, mode="rehab", rehab=res["rehab"], self_perform=res["self_perform"], rehab_label=res["label"],
               defaulted=res["defaulted"], findings=res["findings"], weeks=res["weeks"],
               placeholders=res["placeholders"], warnings=["left out of the plan: " + d for d in res["dropped"]],
               files=[book, walk_name], materials=materials_line())
    return out


def run(task: dict) -> dict:
    import post_walkthrough as pw
    from .offer_sheet import safe_name, warnings_for
    inputs, details = inputs_of(task)
    folder = files_dir(task, NAME)
    out = {"ok": False, "run_dir": str(folder.parent), "files": [], "skipped": [], "warnings": []}
    area = area_inputs(inputs, details)
    if area["why"]:
        out["why"] = area["why"]
        return out
    subj, rec, fields, why = resolve_subject(task, inputs)
    if not subj:
        out["why"] = why
        return out
    out["street"] = subj["street"] or (rec or {}).get("uuid") or "this house"
    out["in_datasift"] = bool(rec)
    notes = area["notes"]
    out["from_notes"] = apply_notes(subj, notes)
    out["context"] = notes.get("context") or ""
    walk_in = None
    if str(inputs.get("walk") or "").strip():
        walk_in, out["walk_from"], why = json_input(inputs["walk"], "walk", task)
        if walk_in is None:
            out["why"] = why
            return out
    if not subj["sqft"]:
        out["why"] = ("there is no square footage for this house; put it in the notes "
                      "(for example 3 bed 1 bath 1,400 sqft built 1950)")
        return out
    walk, held = pinned_walk(subj, walk_in)
    walk, beat = notes_beat_walk(walk, notes)
    if out["context"] and not walk.get("note"):
        walk["note"] = "Operator notes (these beat the photos): " + out["context"]
    notes_lines = ([held] if held else []) + beat
    out.update(held=held, beat=beat, region=walk.get("region"))
    stem = safe_name(subj["street"] or "Deal", "")

    if walk_in is not None and not comps_asked(area):
        return rehab_only(task, subj, walk, folder, stem, notes_lines, out["context"], out)
    walk, out["defaulted"] = pw.one_plan(_pw_subject(subj), walk)
    if not (subj["lat"] and subj["lon"]):
        out["why"] = ("this house is not a DataSift record with a map point, so comps cannot be pulled; "
                      "send the walk with no boundary for the rehab on its own")
        return out
    budget = Budget(task.get("id") or 0, float(setting("AGENTS_DEALS_RUN_CAP_USD", 2.0)))
    try:
        got = comps_for(task, subj, area, budget, cap=int(setting("AGENTS_UNDERWRITE_DEED_READS", 25)))
    except MissingKey as exc:
        out["why"] = str(exc)
        return out
    except Exception as exc:  # noqa: BLE001
        out["why"] = "the comp search failed: %s" % str(exc)[:200]
        return out
    months = int(setting("AGENTS_COMPS_MONTHS", 18))
    lead = lead_for(rec, fields, with_board=True) if rec else {"available": False, "reason": "not a DataSift record"}
    pack = build_pack(subj, lead, got["sold"], got["deeds"]["records"], walk, months, overrides=notes,
                      active=got["active"], land=got["land"])

    names = {"pack": stem + "_pack.json", "walk": stem + "_walk.json", "book": stem + "_Post_Walkthrough.xlsx"}
    (folder / names["pack"]).write_text(json.dumps(pw.pack_to_json(pack), indent=1, default=str), encoding="utf-8")
    (folder / names["walk"]).write_text(json.dumps(walk, indent=1, default=str), encoding="utf-8")
    pw.build_workbook(pack, str(folder / names["book"]))
    files = [names["book"], names["pack"], names["walk"]]
    value, n, how = arv_of(pack)
    rehab, rehab_label = rehab_pick(pack, (walk.get("single_scenario") or {}).get("key") or "mid")
    exits = pack.get("exits") or {}
    offer = None
    if walk_in is not None:
        import offer_sheet as osheet
        # The pack goes through its saved form, exactly as post_walkthrough --save-pack hands it to
        # offer_sheet.py on the workstation, so the two paths cannot price differently.
        saved = json.loads((folder / names["pack"]).read_text(encoding="utf-8"))
        offer = osheet.build_offer(saved, walk, osheet.offer_args(), str(folder / (stem + "_Offer.xlsx")))
        files.insert(0, Path(offer["out"]).name)
        out["warnings"] = warnings_for(offer, walk)
    else:
        out["warnings"] = ["left out of the plan: " + d for d in pw.dropped_walk_items(walk)]
    z = got["zillow"]
    out.update(ok=True, mode="full" if walk_in is not None else "prewalk", value=value, comps_n=n, value_how=how,
               rehab=rehab, rehab_label=rehab_label,
               as_is=int(float((exits.get("inputs") or {}).get("as_is_point") or 0)),
               suggested=[x.get("name") for x in exits.get("suggested") or [] if x.get("name")],
               offer={k: offer[k] for k in ("offer", "buyer_max", "arv", "rehab", "fee", "rule", "scenario")} if offer else None,
               materials=materials_line(), files=files, lead=bool(lead.get("available")), area=area_line(area),
               sold_n=len(got["sold"]))
    out["comps"] = {k: got["deeds"].get(k) for k in ("found", "read", "cached", "not_read", "cap", "why", "months")}
    out["zillow"] = {k: z.get(k) for k in ("why", "calls", "spent_usd", "cached_from", "asked")}
    out["skipped"] = budget.skipped + ([z["why"]] if z.get("why") else [])
    out["spent_usd"] = round(store.spent(task_id=int(task.get("id") or 0)), 2)
    return out


def _context_lines(r: dict) -> list[str]:
    lines = []
    if r.get("from_notes"):
        lines.append("From your notes: %s" % ", ".join(r["from_notes"]))
    if r.get("context"):
        lines.append("Your notes: %s" % r["context"][:300])
    return lines


def render(task: dict, r: dict) -> str:
    mode = r.get("mode") or ""
    head = ":clipboard: *Underwrite: %s*%s" % (r.get("street") or task.get("subject") or "this deal",
                                              "  (rehab only)" if mode == "rehab" else "")
    if not r.get("ok"):
        return safe_text(head + "\nNot built: %s" % (r.get("why") or "no reason given"))
    lines = [head]
    if mode == "rehab":
        lines.append("Rehab *%s* (%s, general contractor model) | self-perform %s | %s weeks" % (
            usd(r["rehab"]), r.get("rehab_label") or "", usd(r.get("self_perform") or 0), r.get("weeks")))
        lines.append("Built from %d finding(s) in the walk%s%s" % (
            r.get("findings") or 0,
            "; %d line(s) in red pending a sub bid" % r["placeholders"] if r.get("placeholders") else "",
            "; the walk named no single plan, so this is the engine's mid plan" if r.get("defaulted") else ""))
        lines.append("No comps were asked for, so there is no value and no offer: paste a boundary, "
                     "or write radius, to add them")
    else:
        if r.get("value"):
            lines.append("Value %s from %d comp(s) (%s) | as-is %s | rehab %s (%s)" % (
                usd(r["value"]), r.get("comps_n") or 0, r.get("value_how") or "deeds",
                usd(r["as_is"]) if r.get("as_is") else "not read", usd(r["rehab"]), r.get("rehab_label") or ""))
        else:
            lines.append("No value: %s" % ((r.get("comps") or {}).get("why") or "not enough comps"))
        if r.get("offer"):
            o = r["offer"]
            lines.append("Offer to seller *%s* (the buyer's most %s, less the %s fee)" % (
                usd(o["offer"]), usd(o["buyer_max"]), usd(o["fee"])))
        else:
            lines.append("No offer sheet: give the walkthrough for a real offer; these are pre-walk numbers")
        if r.get("suggested"):
            lines.append("Exits that clear their gate: %s" % ", ".join(r["suggested"][:4]))
        c = r.get("comps") or {}
        lines.append("Comps %s: %d sold, %d deed(s) read, %d cached%s | rehab materials: %s" % (
            r.get("area") or "", r.get("sold_n") or 0, c.get("read") or 0, c.get("cached") or 0,
            (", %d left unread at the cap" % c["not_read"]) if c.get("not_read") else "", r.get("materials") or "unknown"))
        z = zillow_line(r.get("zillow") or {})
        if z:
            lines.append(z)
    lines += _context_lines(r)
    for w in (list(r.get("beat") or []) + list(r.get("warnings") or []) + list(r.get("skipped") or []))[:3]:
        lines.append(":warning: " + w[:1].upper() + w[1:])
    labels = {"_Offer.xlsx": "Offer sheet", "_Post_Walkthrough.xlsx": "Package", "_Rehab.xlsx": "Rehab workbook",
              "_pack.json": "Pack (offer_sheet takes \"task %s\")" % task.get("id"), "_walk.json": "Walk used"}
    for f in (r.get("files") or [])[:4]:
        label = next((v for k, v in labels.items() if f.endswith(k)), "File")
        lines.append(file_line(task, label, f))
    lines.append("Nothing written to DataSift%s." % (", spent $%.2f" % r["spent_usd"] if r.get("spent_usd") else ""))
    return safe_text("\n".join(lines))
