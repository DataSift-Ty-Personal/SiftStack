"""skill_report: a file skill's thinking step on the file-skill model, then the skill's own generator.

Ty, 2026-11-17: the comping and rehab skills (and the two together) run on the cheapest model that passed
the comps bake-off (src/model_registry.py, job "file_skills"), and real examples are the tests. So every
run checks its own answer before anyone sees it:

  comps  every comp the answer uses is in the data with the same price and size; each bucket's median is
         the median of the comps the answer put in it; the base ARV is the renovated $/sf times the
         subject's size. Renovated comps off the subject's bedroom count and a bucket spread past 30% are
         noted for the reader, not refused (the skill allows both with a reason).
  rehab  every included line is its quantity times its unit cost; each category, the subtotal, the
         contingency and the grand total add up; the deal's rehab cost is that grand total.
  both   the skill's own generator builds the workbook from the answer, or the answer is refused.

An answer that fails goes back once with its problems named; a second failure goes once to the fallback
model (job "file_skills_fallback"); a third tells the employee what was wrong, and she works the skill by
hand the way Claude Code does. Each call's true cost (OpenRouter bills it back on the call) is booked
against the day's cap, and a call that could not fit under the cap is never made.

The engine's tight ARV is never shown to the model: in the bake-off four models copied it as their own
answer when they saw it. It is added to the workbook's notes and the summary afterwards, so the operator
still gets both numbers side by side (his call, 2026-11-17).

One OpenRouter request per attempt, data_collection deny and require_parameters on, so a host that keeps
prompts or drops the JSON setting is never chosen. Nothing is written to DataSift.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import model_registry

from . import config, store

log = logging.getLogger(__name__)

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_SUFFIX = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif"}

# Tests swap this for a fake: (body dict, key) -> (HTTP status, parsed JSON).
POST = None


def _num_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


CALL_CEILING_USD = _num_env("AGENTS_SKILL_CALL_USD", 1.0)    # the most one call can cost, held against the day cap
MAX_PHOTOS = int(_num_env("AGENTS_SKILL_MAX_PHOTOS", 24))
PHOTO_BYTES = int(_num_env("AGENTS_SKILL_PHOTO_MB", 12) * 1024 * 1024)
MAX_TOKENS = int(_num_env("AGENTS_SKILL_MAX_TOKENS", 64000))

COMPS_RULES = [
    "As-is means what a cash investor pays for the house in today's condition: the investor and distressed "
    "purchases (cash or company buyers, priced below the retail band). Dated retail sales are not as-is comps. "
    "Bucket A is that investor tier; say so when the spread runs past 30%.",
    "Price the house that exists: a 2-bed is valued from 2-bed sales (the bedroom band). More bedrooms, baths "
    "or square feet are upside only after a walkthrough proves the layout converts, never part of the ARV.",
    "The comps are county deed sales inside the operator's area (no MLS, no Zillow). Copy each comp's address, "
    "sale price, date, beds, baths and square feet exactly as the data gives them. Never invent a comp, a sale "
    "or a fact; when something is missing, say so in caveats.",
    "Round the values you work out (the ARV, the as-is value, any offer) to the nearest $1,000. Never round a "
    "recorded sale price.",
]
REHAB_RULES = [
    "One rehab number: the scope that makes the assignment work for a flipper or wholesaler at the finish the "
    "comps actually sold with. No menu of scenarios. Leave wholetail_estimate with empty categories and zero "
    "totals unless the operator asked for a wholetail.",
    "Price the house that exists: never add bedrooms, bathrooms or square feet unless the operator says the "
    "walkthrough proved it.",
    "In Knox and Blount counties the materials come from the locked master material list, which is already "
    "local: never multiply those prices by a regional factor (the factor is for labor only).",
    "A cost you cannot see yet (a sub quote pending, a system nobody opened) gets a realistic placeholder named "
    "as one in its notes, never a blank or a zero.",
    "In recommendations, state the assignment math as single figures: the flipper's maximum offer is 70% of the "
    "ARV minus the rehab, and our buy price is that maximum minus a $15,000 fee. One number each, never a range.",
    "Every included line's total is its quantity times its unit cost, and every total adds up exactly. In the "
    "summary, write whole dollars, never cents.",
]

SKILLS = {
    "real-estate-comping": dict(
        short="Comps", generator="scripts/generate_excel_report.py",
        refs=["references/adjustment-cheatsheet.md"],
        shape="the skill's \"Data Structure for Report Generation\" shape (subject_property, comps, "
              "outlier_screening, bucket_analysis, market_sentiment, market_overview, arv_calculation, "
              "adjustments_applied, sources, search_parameters, recommendations, caveats). Put the comps you "
              "actually use in comps (both buckets), each with condition 'Renovated' or 'Unrenovated'.",
        summary="the Two-Bucket ARV, the as-is value, how many comps each rests on, and the caveats",
        rules=COMPS_RULES),
    "rehab-estimator": dict(
        short="Rehab", generator="scripts/generate_rehab_excel.py",
        refs=["references/finish-tiers.md", "references/local-pricing-guide.md",
              "references/real-deal-calibration.md", "references/rehab-categories.md",
              "references/wholetail-vs-rehab.md"],
        shape="the skill's Step 6 \"Data Structure\" (property, comp_reference, condition_assessment, "
              "local_pricing, rehab_estimate, wholetail_estimate, deal_analysis, material_specs, "
              "recommendations, caveats).",
        summary="the one rehab number and its $/sf, the three biggest categories, what would move it, and the "
                "caveats",
        rules=REHAB_RULES),
}
LOCKED_LIST = "data/master_material_list_37914.csv"
LOCKED_COUNTIES = ("knox", "blount")


class Refused(Exception):
    pass


# ------------------------------------------------------------------------------------------ inputs
def skill_dir(skill: str) -> Path:
    return Path(config.SKILLS_DIR) / skill


def non_disclosure_states() -> set[str]:
    """Read off the skill's own quick lookup table, so the list can never drift from the skill."""
    p = skill_dir("real-estate-comping") / "references" / "state-list.md"
    try:
        return set(re.findall(r"(?m)^\|\s*([A-Z]{2})\s*\|\s*Non-Disclosure", p.read_text(encoding="utf-8")))
    except OSError:
        return set()


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _system(skill: str, refs: list[str]) -> str:
    d = skill_dir(skill)
    parts = ["You are Roma, the deal analyst on a real estate investing team. Work the skill below exactly as "
             "written, on the data you are given. Never invent a comp, a sale, a price or a fact.",
             "# SKILL.md\n" + _read(d / "SKILL.md")]
    for r in refs:
        if (d / r).is_file():
            parts.append("# %s\n%s" % (r, _read(d / r)))
    return "\n\n".join(parts)


def _newest(folder: Path, pattern: str) -> Path | None:
    hits = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    return hits[0] if hits else None


def _resolve(task: dict, employee_id: str, given) -> list[Path]:
    from . import sandbox
    out = []
    for g in (given or []):
        try:
            out.append(sandbox.resolve_input(str(g), task, employee_id))
        except sandbox.Refused as exc:
            raise Refused(str(exc)) from None
    return out


def data_files(task: dict, employee_id: str, given) -> tuple[Path | None, Path | None, list[Path]]:
    """The comps CSV and subject JSON from comps_data (named, or this turn's newest), plus any other files."""
    from . import sandbox
    named = _resolve(task, employee_id, given)
    comps = next((p for p in named if p.suffix.lower() == ".csv" and p.name.startswith("comps_")), None)
    subj = next((p for p in named if p.suffix.lower() == ".json" and p.name.startswith("subject_")), None)
    exports = sandbox.run_dir_for(task) / "exports"
    comps = comps or _newest(exports, "comps_*.csv")
    subj = subj or _newest(exports, "subject_*.json")
    others = [p for p in named if p not in (comps, subj)]
    return comps, subj, others


def thread_photos(task: dict, employee_id: str, given) -> list[Path]:
    """The photos saved for this thread in the employee's workspace (or the ones named), oldest first."""
    from . import attachments
    if isinstance(given, list):
        paths = [p for p in _resolve(task, employee_id, given) if p.suffix.lower() in IMAGE_SUFFIX]
    elif str(given or "all").lower() == "none":
        return []
    else:
        d = (Path(config.WORKSPACES_DIR) / employee_id / attachments.INBOX
             / attachments.thread_dir(task.get("slack_channel") or "", task.get("slack_thread") or ""))
        paths = sorted((p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIX),
                       key=lambda p: p.name) if d.is_dir() else []
    out, used = [], 0
    for p in paths:
        size = p.stat().st_size
        if len(out) >= MAX_PHOTOS or used + size > PHOTO_BYTES:
            break
        out.append(p)
        used += size
    return out


def _image_part(p: Path) -> dict:
    mime = IMAGE_SUFFIX[p.suffix.lower()]
    return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (
        mime, base64.b64encode(p.read_bytes()).decode("ascii"))}}


# ------------------------------------------------------------------------------------------ prompts
def _contract(spec: dict) -> list[str]:
    return ["Return ONE JSON object, nothing else, with two keys:",
            '  "report": the analysis data exactly in %s' % spec["shape"],
            '  "summary": a list of at most 12 short lines for the operator in Slack: %s.' % spec["summary"],
            "The skill's generator builds the workbook from your report after you answer, so never say in the "
            "summary whether a workbook or a file was made. Never write an em dash or an en dash anywhere: use "
            "commas, colons or parentheses, and a plain hyphen for a number range."]


DASH_RANGE = re.compile(r"(?<=\d)\s*\u2013\s*(?=\d|\$)")     # an en dash between numbers is a range
DASH_ANY = re.compile(r"\s*[\u2013\u2014]\s*")                # an em dash, or any other en dash, is a break
NO_FILE = re.compile(r"\b(no|not|nothing)\b[^.]{0,60}\b(workbook|pdf|file|spreadsheet)s?\b|"
                     r"\b(workbook|pdf|file|spreadsheet)s?\b[^.]{0,60}\b(not|never)\b[^.]{0,20}\b(generat|creat|made|built)",
                     re.I)


def undash(v):
    """Ty's rule for every word SiftStack writes: no em or en dashes. A dash between numbers is a range."""
    if isinstance(v, str):
        return DASH_ANY.sub(", ", DASH_RANGE.sub("-", v))
    if isinstance(v, list):
        return [undash(x) for x in v]
    if isinstance(v, dict):
        return {k: undash(x) for k, x in v.items()}
    return v


CENTS = re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})\b")


def _whole(m) -> str:
    """$53,708.40 reads as fake precision in a Slack line; the workbook keeps the exact figures."""
    return "$" + format(int(round(float(m.group(1).replace(",", "") + "." + m.group(2)))), ",")


def clean_summary(lines) -> list[str]:
    out = []
    for x in (lines if isinstance(lines, list) else [lines] if lines else []):
        s = CENTS.sub(_whole, undash(str(x))).strip()
        if s and not NO_FILE.search(s):
            out.append(s)
    return out[:12]


def comps_messages(subject_doc: dict, comps_csv: str, notes: str) -> list[dict]:
    s = dict(subject_doc.get("subject") or {})
    subject = {k: s.get(k) for k in ("street", "city", "state", "zip", "county", "beds", "baths", "sqft",
                                     "year_built", "lot_size", "structure_type") if s.get(k) not in (None, "")}
    state = str(s.get("state") or "").upper()
    nd = state in non_disclosure_states()
    refs = list(SKILLS["real-estate-comping"]["refs"]) + [
        "references/non-disclosure-prompt.md" if nd else "references/disclosure-prompt.md"]
    rows = list(csv.DictReader(io.StringIO(comps_csv)))
    user = [
        "Run the real-estate-comping skill for this subject. %s is a %s state, so this is %s." % (
            state or "The subject's state", "non-disclosure" if nd else "disclosure",
            "the Triangulation method" if nd else "Two-Bucket"),
        "", "Subject (county card facts):", json.dumps(subject)]
    if notes:
        user += ["", "The operator's notes (they beat the record):", notes]
    user += ["", "Search: deed sales inside the operator-drawn area, last %s months, %d rows below." % (
        subject_doc.get("months") or "?", len(rows)),
        "", "Comps CSV:", comps_csv, "", "House rules for this team:"] + ["- " + r for r in COMPS_RULES] + [""]
    user += _contract(SKILLS["real-estate-comping"])
    return [{"role": "system", "content": _system("real-estate-comping", refs)},
            {"role": "user", "content": "\n".join(user)}]


def rehab_messages(subject_doc: dict | None, notes: str, photos: list[Path], arv, price, others: list[Path]) -> list[dict]:
    spec = SKILLS["rehab-estimator"]
    s = dict((subject_doc or {}).get("subject") or {})
    subject = {k: s.get(k) for k in ("street", "city", "state", "zip", "county", "beds", "baths", "sqft",
                                     "year_built", "lot_size", "structure_type") if s.get(k) not in (None, "")}
    county = str(s.get("county") or "").lower()
    if not county:
        # A rehab asked without a comps run has no subject file, so the county comes from the notes.
        if re.search(r"\b(knox|knoxville|blount|maryville|alcoa)\b|\b37[789]\d\d\b", notes or "", re.I):
            county = "knox or blount (from the notes)"
    user = ["Run the rehab-estimator skill for this house."]
    if subject:
        user += ["", "Subject (county card facts):", json.dumps(subject)]
    if notes:
        user += ["", "The operator's walkthrough notes (they beat the record and the photos):", notes]
    if arv or price:
        user += ["", "For the deal analysis: ARV %s, purchase price %s." % (
            "$%s" % format(int(arv), ",") if arv else "not given",
            "$%s" % format(int(price), ",") if price else "not given")]
    for p in others[:4]:
        if p.suffix.lower() in (".csv", ".json", ".txt", ".md"):
            user += ["", "File %s:" % p.name, _read(p)[:20000]]
    if any(c in county for c in LOCKED_COUNTIES) and (skill_dir("rehab-estimator") / LOCKED_LIST).is_file():
        user += ["", "The locked master material list (Knox pricing, already local):",
                 _read(skill_dir("rehab-estimator") / LOCKED_LIST)]
    user += ["", "Photos: %d from the walkthrough follow, in the order they were taken." % len(photos)
             if photos else "No photos on this thread: work from the notes and say so in caveats."]
    user += ["", "House rules for this team:"] + ["- " + r for r in REHAB_RULES] + [""] + _contract(spec)
    content: list = [{"type": "text", "text": "\n".join(user)}] + [_image_part(p) for p in photos]
    return [{"role": "system", "content": _system("rehab-estimator", spec["refs"])},
            {"role": "user", "content": content}]


# ------------------------------------------------------------------------------------------ the call
def _post(body: dict, key: str) -> tuple[int, dict]:
    if POST is not None:
        return POST(body, key)
    import requests
    r = requests.post(OPENROUTER, timeout=600, json=body,
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": {"message": r.text[:300]}}


def call(model: str, messages: list[dict]) -> dict:
    """One answer. {"text", "usd", "host", "finish", "seconds"}; raises Refused when nothing came back."""
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        raise Refused("OPENROUTER_API_KEY is not set on this box")
    body = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages,
            "response_format": {"type": "json_object"}, "reasoning": {"effort": "high"},
            "provider": {"data_collection": "deny", "require_parameters": True}, "usage": {"include": True}}
    t0, last = time.time(), ""
    for attempt in range(4):
        try:
            status, j = _post(body, key)
        except Exception as exc:  # noqa: BLE001  (a network fault is retried, then said)
            last = "network: %s" % type(exc).__name__
            time.sleep(5 * (attempt + 1) if POST is None else 0)
            continue
        if status == 200 and j.get("choices"):
            ch = j["choices"][0]
            u = j.get("usage") or {}
            return {"text": (ch.get("message") or {}).get("content") or "", "usd": float(u.get("cost") or 0.0),
                    "host": j.get("provider") or "", "finish": ch.get("finish_reason") or "",
                    "seconds": round(time.time() - t0, 1)}
        msg = json.dumps(j.get("error") or j)[:300]
        last = "HTTP %s %s" % (status, msg)
        if status == 400 and "reasoning" in body and "reason" in msg.lower():
            body.pop("reasoning")          # a host that refuses the reasoning knob: run it plain
            continue
        if status in (408, 429, 500, 502, 503, 504, 529):
            time.sleep(8 * (attempt + 1) if POST is None else 0)
            continue
        break
    raise Refused("the model call failed: %s" % last)


def parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", (text or "").strip(), re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# ------------------------------------------------------------------------------------------ checks
def _n(v) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _addr(a) -> str:
    return re.sub(r"[^a-z0-9]", "", str(a or "").split(",")[0].lower())


def _close(a: float, b: float, rel: float, floor: float) -> bool:
    return abs(a - b) <= max(floor, rel * max(abs(a), abs(b)))


def check_comps(report: dict, comps_csv: str, subject_doc: dict) -> tuple[list[str], list[str], dict]:
    """(problems that refuse the answer, notes for the reader, derived figures)."""
    rows: dict[str, list] = {}
    for r in csv.DictReader(io.StringIO(comps_csv)):
        rows.setdefault(_addr(r.get("Address")), []).append(r)
    comps = [c for c in (report.get("comps") or []) if isinstance(c, dict)]
    subj = dict(subject_doc.get("subject") or {})
    gla = _n((report.get("subject_property") or {}).get("gla")) or _n(subj.get("sqft")) or 0.0
    problems, notes = [], []
    if not comps:
        problems.append("the report lists no comps")
    for c in comps:
        got = rows.get(_addr(c.get("address")))
        if not got:
            problems.append("%s is not in the comp data" % c.get("address"))
            continue
        price = _n(c.get("sale_price")) or 0.0
        if not any(_close(price, _n(r.get("Price")) or 0.0, 0.005, 1.0) for r in got):
            problems.append("%s: sale price %s is not the recorded %s" % (c.get("address"), int(price), got[0].get("Price")))
            continue
        sq = _n(c.get("gla"))
        if sq and not any(_n(r.get("Sqft")) and _close(sq, _n(r.get("Sqft")), 0.01, 1.0) for r in got):
            problems.append("%s: size %s is not the recorded %s" % (c.get("address"), int(sq), got[0].get("Sqft")))

    def ppsf(c):
        return _n(c.get("ppsf")) or ((_n(c.get("sale_price")) or 0.0) / max(_n(c.get("gla")) or 1.0, 1.0))

    use = [c for c in comps if not c.get("outlier_flag")]
    ren = [c for c in use if str(c.get("condition", "")).lower().startswith("renov")]
    unr = [c for c in use if not str(c.get("condition", "")).lower().startswith("renov")]
    buckets = report.get("bucket_analysis") or {}
    ren_med = _n((buckets.get("renovated") or {}).get("median_ppsf"))
    unr_med = _n((buckets.get("unrenovated") or {}).get("median_ppsf"))
    if ren and ren_med and not _close(statistics.median([ppsf(c) for c in ren]), ren_med, 0.05, 1.0):
        problems.append("the renovated median $/sf is %.0f but its own renovated comps give %.0f" % (
            ren_med, statistics.median([ppsf(c) for c in ren])))
    if unr and unr_med and not _close(statistics.median([ppsf(c) for c in unr]), unr_med, 0.05, 1.0):
        problems.append("the unrenovated median $/sf is %.0f but its own unrenovated comps give %.0f" % (
            unr_med, statistics.median([ppsf(c) for c in unr])))
    arv = report.get("arv_calculation") or {}
    base, final, rppsf = _n(arv.get("base_arv")), _n(arv.get("final_arv")), _n(arv.get("renovated_ppsf"))
    if base and rppsf and gla and not _close(rppsf * gla, base, 0.03, 1.0):
        problems.append("the base ARV %.0f is not the renovated $/sf %.2f times %d sq ft" % (base, rppsf, gla))
    if not (final or base):
        problems.append("the report has no ARV")
    beds = _n(subj.get("beds"))
    if beds and ren:
        off = [c.get("address") for c in ren if _n(c.get("beds")) is not None and _n(c.get("beds")) != beds]
        if off:
            notes.append("%d renovated comp(s) have a different bedroom count from the subject (%s)" % (
                len(off), ", ".join(str(a) for a in off[:3])))
    if ren_med and unr_med and unr_med > 0 and ren_med / unr_med - 1 > 0.30:
        notes.append("the two buckets are %d%% apart; by house rule Bucket A is the investor tier, so the gap is "
                     "the rehab and the investor's margin" % round(100 * (ren_med / unr_med - 1)))
    as_is = _n(arv.get("as_is_value")) or (round(unr_med * gla, -3) if unr_med and gla else None)
    derived = {"arv": final or base, "as_is": as_is,
               "comps": len(comps), "renovated": len(ren), "unrenovated": len(unr)}
    return problems, notes, derived


def _check_estimate(est: dict, label: str, problems: list[str]) -> None:
    cats = [c for c in (est.get("categories") or []) if isinstance(c, dict)]
    cat_sum = 0.0
    for c in cats:
        item_sum = 0.0
        for it in c.get("items") or []:
            if not isinstance(it, dict) or it.get("include") is False:
                continue
            qty, unit, tot = _n(it.get("qty")), _n(it.get("unit_cost")), _n(it.get("total")) or 0.0
            if qty is not None and unit is not None and not _close(qty * unit, tot, 0.01, 5.0):
                problems.append("%s, %s / %s: %s x %s is not the total %s" % (
                    label, c.get("category"), it.get("item"), qty, unit, tot))
            item_sum += tot
        ctot = _n(c.get("category_total")) or 0.0
        if c.get("items") and not _close(item_sum, ctot, 0.01, 5.0):
            problems.append("%s, %s: its lines add to %.0f, not the category total %.0f" % (
                label, c.get("category"), item_sum, ctot))
        cat_sum += ctot
    sub, pct = _n(est.get("subtotal")) or 0.0, _n(est.get("contingency_pct")) or 0.0
    cont, grand = _n(est.get("contingency")) or 0.0, _n(est.get("grand_total")) or 0.0
    if cats and not _close(cat_sum, sub, 0.01, 10.0):
        problems.append("%s: the categories add to %.0f, not the subtotal %.0f" % (label, cat_sum, sub))
    if pct and not _close(sub * pct, cont, 0.01, 5.0):
        problems.append("%s: %.0f%% of the subtotal is %.0f, not the contingency %.0f" % (label, 100 * pct, sub * pct, cont))
    if (sub or cont) and not _close(sub + cont, grand, 0.01, 10.0):
        problems.append("%s: subtotal plus contingency is %.0f, not the grand total %.0f" % (label, sub + cont, grand))


def check_rehab(report: dict) -> tuple[list[str], list[str], dict]:
    problems: list[str] = []
    est = report.get("rehab_estimate") or {}
    if not isinstance(est, dict) or not est.get("categories"):
        problems.append("the report has no rehab_estimate categories")
        est = est if isinstance(est, dict) else {}
    else:
        _check_estimate(est, "rehab", problems)
    wt = report.get("wholetail_estimate")
    if isinstance(wt, dict) and wt.get("categories"):
        _check_estimate(wt, "wholetail", problems)
    grand = _n(est.get("grand_total"))
    deal = report.get("deal_analysis") or {}
    rc = _n(deal.get("rehab_cost"))
    if grand and rc and not _close(grand, rc, 0.01, 10.0):
        problems.append("the deal analysis uses a rehab of %.0f, not the estimate's %.0f" % (rc, grand))
    cats = sorted(((c.get("category"), _n(c.get("category_total")) or 0.0) for c in est.get("categories") or []
                   if isinstance(c, dict)), key=lambda x: -x[1])
    return problems, [], {"rehab": grand, "per_sf": _n(est.get("cost_per_sf")), "top": cats[:3]}


# ------------------------------------------------------------------------------------------ the workbook
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(s or "")).strip("_")[:60] or "house"


def _free_name(folder: Path, stem: str, suffix: str) -> str:
    name, n = stem + suffix, 2
    while (folder / name).exists():
        name, n = "%s_%d%s" % (stem, n, suffix), n + 1
    return name


def render(skill: str, report: dict, task: dict, address: str) -> tuple[bool, str, Path | None]:
    """Run the skill's own generator on the answer. (ok, message, workbook path)."""
    from . import sandbox
    spec = SKILLS[skill]
    gen = skill_dir(skill) / spec["generator"]
    if not gen.is_file():
        return False, "the skill's generator %s is missing on this box" % spec["generator"], None
    out_dir = sandbox.run_dir_for(task) / "files"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _free_name(out_dir, "%s_%s" % (spec["short"], _slug(address)), ".xlsx")
    exports = sandbox.run_dir_for(task) / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    data = exports / (Path(name).stem + "_data.json")
    data.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME")
    env = {k: os.environ[k] for k in keep if os.environ.get(k)}   # no secrets in the child process
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryDirectory() as cwd:
        try:
            p = subprocess.run([sys.executable, str(gen), str(out_dir / name), str(data)], cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return False, "the generator ran past 180 seconds", None
    if p.returncode != 0 or not (out_dir / name).is_file():
        return False, "the generator failed: %s" % ((p.stderr or p.stdout or "").strip()[-400:]), None
    return True, "", out_dir / name


# ------------------------------------------------------------------------------------------ the run
def _room_today() -> float:
    """What is left of the team's day cap. Research has its own cap and is not counted here."""
    return config.DAILY_CAP_USD - store.spent(day=store.today(), research=False)


def run(task: dict, employee_id: str, args: dict) -> dict:
    """skill_report. Never raises: {"ok": True, ...} or {"ok": False, "why", "problems"}."""
    a = dict(args or {})
    skill = str(a.get("skill") or "").strip()
    if skill not in SKILLS:
        return {"ok": False, "why": "skill is one of %s" % ", ".join(SKILLS)}
    if not os.environ.get("OPENROUTER_API_KEY"):
        return {"ok": False, "why": "OPENROUTER_API_KEY is not set on this box, so the file-skill model cannot be "
                "reached; work the skill by hand", "problems": []}
    t0, spent, tried = time.time(), 0.0, []
    try:
        comps_p, subj_p, others = data_files(task, employee_id, a.get("files"))
        subject_doc = json.loads(_read(subj_p)) if subj_p else {}
        address = str(a.get("address") or (subject_doc.get("subject") or {}).get("street") or "house")
        notes = str(a.get("notes") or "").strip()
        if skill == "real-estate-comping":
            if not comps_p or not subj_p:
                raise Refused("there is no comp data on this turn yet: run comps_data first")
            comps_csv = _read(comps_p)
            messages = comps_messages(subject_doc, comps_csv, notes)
        else:
            comps_csv = ""
            photos = thread_photos(task, employee_id, a.get("photos"))
            if not photos and not notes:
                raise Refused("a rehab needs the walkthrough photos on the thread or the operator's notes")
            messages = rehab_messages(subject_doc or None, notes, photos, _n(a.get("arv")),
                                      _n(a.get("purchase_price")), others)
    except Refused as exc:
        return {"ok": False, "why": str(exc), "problems": []}
    except (OSError, ValueError) as exc:
        return {"ok": False, "why": "the skill's inputs could not be read: %s" % exc, "problems": []}

    plan = [model_registry.model("file_skills"), model_registry.model("file_skills"),
            model_registry.model("file_skills_fallback")]
    problems: list[str] = []
    convo = list(messages)
    for step, model in enumerate(plan):
        if step == 2:
            convo = list(messages)          # the fallback model starts clean, with the problems named
            if problems:
                convo = messages + [{"role": "user", "content": "Another analyst's answer failed these checks; "
                                     "avoid them: " + "; ".join(problems[:12])}]
        if _room_today() < CALL_CEILING_USD:
            return {"ok": False, "why": "today's AI spend is too close to the team's day cap ($%.2f; research has its "
                    "own) for another skill call; work the skill by hand or wait for tomorrow" % config.DAILY_CAP_USD,
                    "problems": problems, "usd": round(spent, 4)}
        try:
            got = call(model, convo)
        except Refused as exc:
            tried.append({"model": model, "error": str(exc)[:200]})
            problems = [str(exc)]
            continue
        spent += got["usd"]
        store.add_spend(int(task.get("id") or 0), got["usd"], "skill %s (%s)" % (skill, model))
        obj = undash(parse(got["text"]))
        report = (obj or {}).get("report") if obj else None
        if not isinstance(report, dict):
            problems = ["the answer was not one JSON object with a report (finish: %s)" % (got["finish"] or "?")]
            tried.append({"model": model, "problems": problems, "usd": got["usd"]})
            continue
        if skill == "real-estate-comping":
            problems, notes_out, derived = check_comps(report, comps_csv, subject_doc)
        else:
            problems, notes_out, derived = check_rehab(report)
        if not problems:
            if skill == "real-estate-comping":
                engine = subject_doc.get("engine") or {}
                if engine.get("tight_arv"):
                    line = "Engine tight ARV $%s (%s comps; %s)" % (
                        format(int(engine["tight_arv"]), ","), engine.get("comps_used") or "?", engine.get("basis") or "")
                    mo = report.setdefault("market_overview", {})
                    if isinstance(mo, dict):
                        mo["notes"] = ("%s %s" % (mo.get("notes") or "", line)).strip()
                    derived["engine_arv"] = engine["tight_arv"]
            ok, why, path = render(skill, report, task, address)
            if ok:
                from . import slack
                tried.append({"model": model, "usd": got["usd"], "seconds": got["seconds"]})
                return {"ok": True, "skill": skill, "model": model, "fallback_used": step == 2, "attempts": step + 1,
                        "seconds": round(time.time() - t0, 1), "usd": round(spent, 4),
                        "summary": clean_summary(obj.get("summary")), "notes": notes_out,
                        "figures": derived, "file": {"name": path.name, "link": slack.file_url(int(task.get("id") or 0), path.name)},
                        "publish": "give publish_sheet file=%s to make it a Google Sheet" % path.name}
            problems = [why]
        tried.append({"model": model, "problems": problems[:8], "usd": got["usd"]})
        if step == 0:
            convo = messages + [{"role": "assistant", "content": got["text"][:60000]},
                                {"role": "user", "content": "Your answer failed these checks: " + "; ".join(problems[:12])
                                 + ". Fix them and reply with the whole corrected JSON object."}]
    log.warning("skill_report %s gave up after %s", skill, tried)
    return {"ok": False, "why": "the answer failed its checks on both models; work the skill by hand the way the "
            "Skills rules say", "problems": problems[:12], "tried": tried, "usd": round(spent, 4)}
