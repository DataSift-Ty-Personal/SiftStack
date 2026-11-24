"""SmartSkip family trees for the ranked houses: each title holder traced at their mailing address, so SmartSkip
returns the family (relatives, their possible relationship, age, deceased flag, phones). Uses the user's own
SmartSkip account (SMARTSKIP_EMAIL / SMARTSKIP_PASSWORD in .env).

Dry by default: the batch file is built and SmartSkip's free quote is read. --commit pays (SmartSkip bills about
$0.15 a hit on the saved card), waits, downloads and parses. A paid order is remembered before it is paid, so a
run that stops part way downloads it next time instead of paying twice.

SmartSkip's relationship labels are a guess (most read Relative or In-Law) and its deceased flags run late; the
heir research settles both. A deceased flag on a title holder still counts as the death.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from collections import defaultdict

from . import common as C

SS_DIR = C.ROOT / "skills" / "deep-prospecting-v5" / "scripts"
COLS = ["First Name", "Middle Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip"]
DONE_TAGS = {"SmartSkip Skipped", "Deep Prospected", "Heirs Resolved"}
HIT_USD = 0.15


def _modules(run_dir):
    """The SmartSkip client from the deep prospecting skill, with this batch's run folder and the account's own
    SmartSkip sign-in (from .env, never printed)."""
    env = C.load_env()
    for k in ("SMARTSKIP_EMAIL", "SMARTSKIP_PASSWORD"):
        if env.get(k) and not os.environ.get(k):
            os.environ[k] = env[k]
    os.environ["SKIPTRACE_RUN_DIR"] = str(run_dir)
    if str(SS_DIR) not in sys.path:
        sys.path.insert(0, str(SS_DIR))
    import parse_smartskip as ps
    import smartskip_trace as ss
    ss.RUN_DIR = str(run_dir)
    return ss, ps


def people_of(r: dict) -> list[tuple[str, str, str]]:
    out = []
    if r.get("decedent_court"):
        p = C.split_name(r["decedent_court"])
        if p:
            out.append(p)
    for d in (r.get("deed") or [])[:2]:
        p = C.split_name(d)
        if p and p not in out:
            out.append(p)
    if not out:
        for d in (r.get("roll_people") or [])[:2]:
            p = C.split_roll_name(d)
            if p:
                out.append(p)
    return out[:2]


def picks(scored: list[dict], have: dict, top: int = 0, tiers=("1 Work now", "2 Research first")) -> list[dict]:
    """The batch rows: houses in the given tiers (or the top N not ruled out) with no family tree yet."""
    rows, n = [], 0
    for r in scored:
        if r.get("tier") == "Out":
            continue
        if top:
            n += 1
            if n > top:
                break
        elif r.get("tier") not in tiers:
            continue
        if r["key"] in have or (DONE_TAGS & set(r.get("tags") or [])) or r.get("research_status"):
            continue
        base = {"Mailing Address": r.get("street"), "Mailing City": r.get("city"), "Mailing State": r.get("state") or "TN",
                "Mailing Zip": str(r.get("zip") or "")[:5], "Property Address": r.get("street"), "Property City": r.get("city"),
                "Property State": r.get("state") or "TN", "Property Zip": str(r.get("zip") or "")[:5]}
        for first, mid, last in people_of(r):
            rows.append(dict(base, **{"First Name": first, "Middle Name": mid, "Last Name": last, "_key": r["key"]}))
    return rows


def order(ws, top: int = 0, quote: bool = False, commit: bool = False, max_usd: float = 60.0, out=print) -> dict:
    """One SmartSkip batch for the houses in scope with no family tree yet. The batch waits in
    work/smartskip/next/ until it is downloaded, then moves to a dated folder; a paid order that was never
    downloaded is finished before anything new is built."""
    root = ws.work / "smartskip"
    folder = root / "next"
    state = C.read_json(folder / "state.json", {}) or {}
    if state.get("paid") and not state.get("done"):
        rows = C.read_json(folder / "ss_rows.json", []) or []
        out("finishing the paid SmartSkip order %s first" % state.get("bulkSkipId"))
    else:
        rows = picks(ws.read("scored.json", []) or [], ws.read("family.json", {}) or {}, top=top)
        if not rows:
            out("SmartSkip: every house in scope already has a family tree")
            return {"rows": 0}
        if state.get("bulkSkipId") and state.get("rows") != len(rows):
            state = {}                    # the scope moved since the quote: quote again
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "ss_input.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    C.write_json(folder / "ss_rows.json", rows)
    out("SmartSkip batch: %d people at %d houses" % (len(rows), len({r["_key"] for r in rows})))
    if not (commit or quote) and not state.get("bulkSkipId"):
        out("DRY: the batch file is %s. --quote reads SmartSkip's free estimate; --commit pays." % path)
        return {"rows": len(rows), "file": str(path)}
    ss, ps = _modules(folder / ".skiptrace_run")
    sess = ss.Session()
    if not state.get("bulkSkipId"):
        entry = ss.submit(sess, str(path), {})
        state = {"bulkSkipId": entry["bulkSkipId"], "entities": entry.get("entities"), "rows": len(rows), "paid": False}
        C.write_json(folder / "state.json", state)
    ent = int(state.get("entities") or 0)
    out("SmartSkip order %s: %d billable people of %d, at most $%.2f (billed on hits only)"
        % (state["bulkSkipId"], ent, len(rows), ent * HIT_USD))
    if not commit:
        out("DRY: nothing billed. --commit pays (cap --max-usd %.0f)." % max_usd)
        return {"rows": len(rows), "quote": round(ent * HIT_USD, 2)}
    if not state.get("paid") and ent * HIT_USD > max_usd:
        raise SystemExit("refused: the quote ($%.2f) is over --max-usd %.0f" % (ent * HIT_USD, max_usd))
    if not state.get("paid"):
        if not ss.pay(sess, state["bulkSkipId"]):
            raise SystemExit("SmartSkip did not take the payment (a card check or a decline): finish it in the SmartSkip "
                             "app, then run this again; it downloads the order without paying twice")
        state["paid"] = True
        C.write_json(folder / "state.json", state)
    t0 = time.time()
    while True:
        items = ss.fetch_status(sess, state["bulkSkipId"])
        st = ((items[0].get("status") if items else "") or "").lower()
        if st == "completed":
            break
        if st in ("error", "failed"):
            raise SystemExit("the SmartSkip order ended in status %s" % st)
        if time.time() - t0 > 1800:
            raise SystemExit("still running after 30 minutes; the order is paid and saved: run this again to download it")
        time.sleep(20)
    v = folder / "ss_vertical.csv"
    ss.download(sess, state["bulkSkipId"], str(v), "vertical")
    parsed = ps.parse(str(v))
    C.write_json(folder / "ss_parsed.json", parsed, indent=None)
    hits = sum(1 for p in parsed if p.get("has_results"))
    state.update(hits=hits, usd=round(hits * HIT_USD, 2), done=time.strftime("%Y-%m-%d %H:%M"))
    C.write_json(folder / "state.json", state)
    os.replace(folder, root / time.strftime("%Y%m%d_%H%M%S"))
    out("SmartSkip: %d hits of %d people, about $%.2f" % (hits, len(parsed), hits * HIT_USD))
    build(ws, out=out)
    return {"rows": len(rows), "hits": hits, "usd": state["usd"]}


def _k3(first, last, street):
    def n(s):
        return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())
    return n(first), n(last), n(street)


REL_ORDER = {"Spouse": 0, "Husband": 0, "Wife": 0, "Child": 1, "Son": 1, "Daughter": 1, "Parent": 2, "Mother": 2,
             "Father": 2, "Sibling": 3, "Brother": 3, "Sister": 3, "In-Law": 4}


def tree(subjects: list[dict], relatives: dict) -> dict:
    rels = sorted(relatives.values(), key=lambda x: (REL_ORDER.get(x["rel"], 5), x["name"]))
    spouses = [x for x in rels if x["rel"] in ("Spouse", "Husband", "Wife")]
    kids = [x for x in rels if x["rel"] in ("Child", "Son", "Daughter")]
    parents = [x for x in rels if x["rel"] in ("Parent", "Mother", "Father")]
    sibs = [x for x in rels if x["rel"] in ("Sibling", "Brother", "Sister")]
    if kids or spouses:
        heirs, order_ = spouses + kids, "spouse and children"
    elif parents:
        heirs, order_ = parents, "parents (no spouse or child found)"
    elif sibs:
        heirs, order_ = sibs, "siblings (no spouse, child or parent found)"
    else:
        heirs, order_ = [], "no close family found"
    return {"subjects": subjects, "relatives": rels, "spouses": len(spouses), "children": len(kids),
            "parents": len(parents), "siblings": len(sibs), "heirs": [x["name"] for x in heirs], "heir_order": order_,
            "heirs_n": len(heirs), "with_phones": sum(1 for x in rels if x["phones"]),
            "any_deceased": any(s["deceased"] for s in subjects),
            "all_deceased": bool(subjects) and all(s["deceased"] for s in subjects)}


def build(ws, out=print) -> dict:
    """family.json from every SmartSkip batch on file: per house, the traced title holders and their relatives,
    and the likely heirs in the usual no-will order (spouse and children, else parents, else siblings). A research
    aid, never a legal answer."""
    rows, parsed = [], []
    root = ws.work / "smartskip"
    for d in sorted(root.iterdir()) if root.exists() else []:
        rp, pp = d / "ss_rows.json", d / "ss_parsed.json"
        if rp.exists() and pp.exists():
            rows += C.read_json(rp, []) or []
            parsed += C.read_json(pp, []) or []
    by_mail, by_prop = defaultdict(set), defaultdict(set)
    for r in rows:
        by_mail[_k3(r["First Name"], r["Last Name"], r["Mailing Address"])].add(r["_key"])
        by_prop[_k3(r["First Name"], r["Last Name"], r["Property Address"])].add(r["_key"])
    fam: dict = {}
    unmatched = 0
    for p in parsed:
        keys = (by_mail.get(_k3(p.get("first"), p.get("last"), p.get("mailing_address")))
                or by_prop.get(_k3(p.get("first"), p.get("last"), p.get("property_address"))))
        if not keys:
            unmatched += 1
            continue
        for key in keys:
            f = fam.setdefault(key, {"subjects": [], "relatives": {}})
            f["subjects"].append({"name": ("%s %s" % (p.get("first") or "", p.get("last") or "")).strip(),
                                  "deceased": bool(p.get("deceased")), "phones": len(p.get("subject_phones") or []),
                                  "hit": bool(p.get("has_results"))})
            for rl in p.get("relatives") or []:
                nk = (str(rl.get("first") or "").upper(), str(rl.get("last") or "").upper())
                cur = f["relatives"].get(nk)
                if cur is None or len(rl.get("phones") or []) > len(cur.get("phones") or []):
                    f["relatives"][nk] = {"name": " ".join(w.capitalize() for w in str(rl.get("name") or "").split()),
                                          "rel": rl.get("canon_rel") or "Relative", "type_raw": rl.get("type_raw"),
                                          "age": rl.get("age"), "deceased": rl.get("deceased"),
                                          "street": rl.get("mailing_street") or "", "city": (rl.get("mailing_city") or "").title(),
                                          "state": rl.get("mailing_state") or "", "zip": rl.get("mailing_zip") or "",
                                          "phones": rl.get("phones") or [], "of": str(p.get("first") or "").capitalize()}
    out_ = {k: tree(f["subjects"], f["relatives"]) for k, f in fam.items()}
    ws.write("family.json", out_, indent=None)
    out("family trees: %d house(s), %d relative(s), %d people not joined" % (
        len(out_), sum(len(v["relatives"]) for v in out_.values()), unmatched))
    return out_
