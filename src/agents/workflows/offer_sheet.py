"""offer_sheet: the one-tab wholesale offer for Roma, from a saved pack and a walkthrough.

The default deliverable after a walkthrough (Ty, 2026-08-27): one page that answers OFFER TO SELLER,
laid out like the Fortune Builders analyzer, every figure a live Excel formula. This is
src/offer_sheet.py run on the brain, through the same build_offer() the command line prints from, so
the number posted in Slack is the number on the page.

Where the inputs come from:
  * the pack: "task 41" (the pack that underwrite run saved), or a file name in the employee's
    workspace or the deals folder (AGENTS_DEALS_DIR, /data/deals on the box);
  * the walk: inline JSON, a file name the same way, or nothing (the walk saved beside the pack, else
    the one inside the pack).
Only the employee's workspace, the run folders and the deals folder are opened: a path typed in
Slack must never read the box's own files, so anything else is refused and said.

The stale pack trap is handled where it always was: the rehab is recomputed from the walk, never read
from the pack. A walk with no single_scenario prices off the first of four scenarios (cosmetic), a menu
and not an answer, so the result says so out loud, as it does for a credit or flag the plan drops.

Free: it reads two files and writes one workbook. Nothing goes to DataSift.
"""
from __future__ import annotations

from pathlib import Path

from .quick_screen import file_line, files_dir, find_file, inputs_of, json_input, money, read_json, safe_text, usd

NAME = "offer_sheet"


def safe_name(text: str, suffix: str) -> str:
    stem = "".join(ch for ch in (text or "Deal") if ch.isalnum() or ch in " -")[:44].strip().replace(" ", "_")
    return (stem or "Deal") + suffix


def _rule(v) -> float:
    """'75', '75%' or '0.75' -> 0.75; empty -> 0 (keep the sheet's default)."""
    x = money(str(v or "").replace("%", ""))
    return x / 100.0 if x > 1 else x


def warnings_for(res: dict, walk: dict) -> list[str]:
    import post_walkthrough as pw
    out = []
    if not res.get("arv"):
        out.append("the pack carries no value (ARV), so the offer is not a real number")
    if not walk.get("single_scenario"):
        out.append("the walk sets no single plan (single_scenario), so the rehab is the first of the scenarios "
                   "(%s), a menu and not an answer" % (res.get("scenario") or "unnamed"))
    else:
        out += ["left out of the plan: " + d for d in pw.dropped_walk_items(walk)]
    return out


def run(task: dict) -> dict:
    import offer_sheet as osheet
    inputs, _ = inputs_of(task)
    folder = files_dir(task, NAME)
    out = {"ok": False, "run_dir": str(folder.parent), "files": []}
    pack_path, why = find_file(inputs.get("pack") or "", "pack", task)
    if not pack_path:
        out["why"] = why
        return out
    pack, why = read_json(pack_path)
    if not pack or not pack.get("subject"):
        out["why"] = why or "%s is not a saved pack (it has no subject)" % pack_path.name
        return out
    if str(inputs.get("walk") or "").strip():
        walk, walk_from, why = json_input(inputs["walk"], "walk", task)
        if walk is None:
            out["why"] = why
            return out
    else:
        beside = sorted(pack_path.parent.glob("*_walk.json"))   # an underwrite run keeps its walk with its pack
        found = read_json(beside[0])[0] if beside else None
        walk, walk_from = (found, beside[0].name) if found is not None else (pack.get("walk") or {}, "the walk saved in the pack")
    over = {k: money(inputs.get(k)) for k in ("arv", "as_is", "fee") if money(inputs.get(k))}
    if _rule(inputs.get("rule")):
        over["rule"] = _rule(inputs.get("rule"))
    subject = pack["subject"]
    name = safe_name(subject.get("address") or "Offer", "_Offer.xlsx")
    res = osheet.build_offer(pack, walk, osheet.offer_args(**over), str(folder / name))
    out.update(ok=True, address=subject.get("full_address") or subject.get("address") or "", pack_from=pack_path.name,
               walk_from=walk_from, files=[Path(res["out"]).name],
               **{k: res[k] for k in ("arv", "rehab", "rule", "fee", "buyer_max", "offer", "scenario", "as_is")})
    out["warnings"] = warnings_for(res, walk)
    return out


def render(task: dict, r: dict) -> str:
    head = ":moneybag: *Offer sheet: %s*" % (r.get("address") or task.get("subject") or "this deal")
    if not r.get("ok"):
        return safe_text(head + "\nNot built: %s" % (r.get("why") or "no reason given"))
    lines = [head,
             "Offer to seller *%s* | the buyer's most %s (%d%% of %s less %s of rehab) | fee %s" % (
                 usd(r["offer"]), usd(r["buyer_max"]), round(r["rule"] * 100), usd(r["arv"]), usd(r["rehab"]),
                 usd(r["fee"])),
             "Rehab: %s, from %s" % (r.get("scenario") or "unnamed", r.get("walk_from") or "the pack")]
    for w in (r.get("warnings") or [])[:3]:
        lines.append(":warning: " + w[:1].upper() + w[1:])
    for f in r.get("files") or []:
        lines.append(file_line(task, "Offer sheet", f))
    return safe_text("\n".join(lines))
